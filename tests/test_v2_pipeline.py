import asyncio
import json
import time
from uuid import uuid4

import grpc
import httpx
from ajin_edge.v2 import runtime
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.processing import envelope, utc_now
from ajin_edge.wire import lidar_pb2, lidar_pb2_grpc
from test_v2_processing import configuration


async def test_three_services_configuration_ack_then_raw_and_assessment(tmp_path, monkeypatch):
    config = configuration()
    config["coordinate_frame"].update(
        unit="mm",
        origin_description="Test origin",
        x_axis_description="Test X",
        y_axis_description="Test Y",
        z_axis_description="Up",
        handedness="RIGHT_HANDED",
    )
    registration = {**config, **envelope(config, "edge_configuration")}
    received = []
    registered = False

    async def backend(request):
        nonlocal registered
        body = json.loads(request.content)
        validate_message(body)
        if body["message_type"] == "edge_configuration":
            registered = True
        else:
            assert registered, "normal output preceded remote configuration ACK"
        received.append(body)
        return httpx.Response(
            201,
            json={
                "message_id": body["message_id"],
                "accepted": True,
                "duplicate": False,
                "received_at": utc_now(),
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        runtime.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(backend), **kwargs),
    )

    class Driver(lidar_pb2_grpc.LidarScanSourceServicer):
        def __init__(self, sensor):
            self.sensor = sensor

        async def SubscribeScans(self, request, context):
            instance = str(uuid4())
            sequence = 0
            while True:
                sequence += 1
                frame = lidar_pb2.ScanFrame(
                    schema_version="2.0",
                    edge_id=config["edge_id"],
                    sensor_id=self.sensor,
                    instance_id=instance,
                    sequence=sequence,
                    acquired_at_unix_ms=int(time.time() * 1000),
                    acquired_monotonic_ns=time.monotonic_ns(),
                    config_revision=config["config_revision"],
                    sdk_status="OK",
                    scan_id=f"{instance}:{sequence}",
                    clock_domain_id="boot-test",
                )
                frame.samples.add(angle_mdeg=90000, distance_mm=1000, quality=0)
                yield frame
                await asyncio.sleep(0.1)

    servers = []
    drivers = {}
    for sensor in ("lidar-a", "lidar-b"):
        server = grpc.aio.server()
        lidar_pb2_grpc.add_LidarScanSourceServicer_to_server(Driver(sensor), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        drivers[sensor] = f"127.0.0.1:{port}"
        servers.append(server)

    # Production load_runtime accepts UDS only; TCP loopback here makes CI portable.
    import socket

    def free_endpoint():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return f"127.0.0.1:{sock.getsockname()[1]}"

    (tmp_path / "boot").write_text("boot-test")
    (tmp_path / "token").write_text("test-token")
    runtime_config = {
        "configuration": registration,
        "drivers": drivers,
        "clock_domain_file": str(tmp_path / "boot"),
        "token_file": str(tmp_path / "token"),
        "backend_origin": "https://backend.example",
        "storage_dir": str(tmp_path / "outbox-v2"),
        "status_dir": str(tmp_path / "status"),
        "delivery_endpoint": free_endpoint(),
        "observation_endpoint": free_endpoint(),
    }
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(role(runtime_config, stop))
        for role in (runtime.uplink, runtime.processing, runtime.orchestrator)
    ]
    try:
        for _ in range(80):
            await asyncio.sleep(0.1)
            kinds = {body["message_type"] for body in received}
            scans = [
                s["scan"]
                for body in received
                if body["message_type"] == "lidar_measurement"
                for s in body["sensors"]
                if s["scan"]
            ]
            if (
                kinds
                == {"edge_configuration", "lidar_measurement", "edge_assessment", "edge_status"}
                and scans
            ):
                break
            for task in tasks:
                if task.done():
                    await task
        assert scans
        assert kinds == {
            "edge_configuration",
            "lidar_measurement",
            "edge_assessment",
            "edge_status",
        }
        assert scans[0]["points"][0]["quality"] == 0
        assert all(
            body["fill_estimate"] is None
            for body in received
            if body["message_type"] == "edge_assessment"
        )

        # A slow local status RPC must not delay independent one-second judgments.
        async def slow_stats(self):
            await asyncio.sleep(5)
            return {}

        monkeypatch.setattr(runtime.DeliveryStub, "stats", slow_stats)
        before = sum(body["message_type"] == "edge_assessment" for body in received)
        await asyncio.sleep(2.5)
        after = sum(body["message_type"] == "edge_assessment" for body in received)
        assert after - before >= 2
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        for server in servers:
            await server.stop(0)
