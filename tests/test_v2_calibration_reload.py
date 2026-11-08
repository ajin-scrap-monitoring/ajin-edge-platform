import asyncio
import json
import socket
import time
from uuid import uuid4

import grpc
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from ajin_edge.v2 import runtime
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.ipc import OPTIONS, DeliveryStub
from ajin_edge.v2.processing import utc_now
from ajin_edge.wire import lidar_pb2, lidar_pb2_grpc
from test_v2_calibration_control import base_runtime, calibration_request, control_module


async def test_configuration_replacement_during_startup_cannot_leave_an_old_generation_running(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"generation": "old"}))
    stop = asyncio.Event()
    seen = []
    first = [True]

    def loader(path):
        config = json.loads(path.read_text())
        if first[0]:
            first[0] = False
            path.write_text(json.dumps({"generation": "new"}))
        return config

    async def service(config, generation_stop):
        seen.append(config["generation"])
        stop.set()
        await generation_stop.wait()

    monkeypatch.setattr(runtime, "load_runtime", loader)
    monkeypatch.setattr(runtime, "processing", service)
    await asyncio.wait_for(runtime.run_service("processing", path, stop), 1)
    assert seen == ["new"]


@pytest.mark.parametrize("all_settings", [False, True], ids=["installation", "all-settings"])
async def test_http_calibration_ack_reloads_services_and_produces_verified_z(
    tmp_path, monkeypatch, all_settings
):
    assert hasattr(runtime, "run_service"), "managed configuration reload is missing"
    from ajin_edge.v2.calibration_api import create_app

    base = base_runtime(tmp_path)
    registration = base["configuration"]

    def endpoint():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return f"127.0.0.1:{sock.getsockname()[1]}"

    class Driver(lidar_pb2_grpc.LidarScanSourceServicer):
        def __init__(self, sid):
            self.sid = sid
            self.last_frame = None

        async def SubscribeScans(self, request, context):
            if self.last_frame is not None:
                yield self.last_frame
            instance = str(uuid4())
            seq = 0
            while True:
                seq += 1
                frame = lidar_pb2.ScanFrame(
                    schema_version="2.0",
                    edge_id=registration["edge_id"],
                    sensor_id=self.sid,
                    instance_id=instance,
                    sequence=seq,
                    acquired_at_unix_ms=int(time.time() * 1000),
                    acquired_monotonic_ns=time.monotonic_ns(),
                    config_revision=registration["config_revision"],
                    sdk_status="OK",
                    scan_id=f"{instance}:{seq}",
                    clock_domain_id="boot-test",
                )
                frame.samples.add(angle_mdeg=90000, distance_mm=1000, quality=100)
                if self.last_frame is None:
                    self.last_frame = frame
                yield frame
                await asyncio.sleep(0.1)

    servers = []
    drivers = {}
    for sid in ["lidar-a", "lidar-b"]:
        server = grpc.aio.server()
        lidar_pb2_grpc.add_LidarScanSourceServicer_to_server(Driver(sid), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        servers.append(server)
        drivers[sid] = f"127.0.0.1:{port}"
    base.update(
        drivers=drivers,
        delivery_endpoint=endpoint(),
        observation_endpoint=endpoint(),
        storage_dir=str(tmp_path / "outbox"),
        backend_origin="https://backend.example",
        token_file=str(tmp_path / "backend-token"),
    )
    (tmp_path / "backend-token").write_text("test-backend-token")
    token = tmp_path / "control-token"
    token.write_text("x" * 40)
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(base))
    module = control_module()

    def test_loader(path, *, apply_control=True):
        config = json.loads(config_path.read_text())
        return module.effective_runtime(config) if apply_control else config

    monkeypatch.setattr(runtime, "load_runtime", test_loader)
    received = []

    async def backend(request):
        body = json.loads(request.content)
        validate_message(body)
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
    stop = asyncio.Event()
    tasks = []
    with module.ControlState(base) as state:
        try:
            tasks = [
                asyncio.create_task(runtime.run_service(role, config_path, stop))
                for role in ["uplink", "processing", "orchestrator"]
            ]
            async with grpc.aio.insecure_channel(base["delivery_endpoint"], options=OPTIONS) as ch:
                async with TestClient(
                    TestServer(create_app(state, DeliveryStub(ch), token))
                ) as client:
                    for _ in range(100):
                        if any(b["message_type"] == "lidar_measurement" for b in received):
                            break
                        for task in tasks:
                            if task.done():
                                await task
                        await asyncio.sleep(0.05)
                    headers = {
                        "Authorization": "Bearer " + "x" * 40,
                        "Idempotency-Key": "live-reload",
                    }
                    payload = calibration_request(base)
                    route, jobs = "calibrations", "calibration-jobs"
                    if all_settings:
                        from copy import deepcopy

                        from test_v2_configuration_control import configuration_request

                        payload = configuration_request(base)
                        installation = payload["calibrations"][0]["calibration"]
                        installation.update(
                            rotation=[[1, 0, 0], [0, 0, -1], [0, 1, 0]], translation_mm=[0, 0, 0]
                        )
                        payload["calibrations"].append(
                            {"sensor_id": "lidar-b", "calibration": deepcopy(installation)}
                        )
                        for section in payload["judgment"]["sections"]:
                            section.update(floor_z_mm=[0], ceiling_z_mm=[2000], active_bins=[True])
                        components = payload["judgment"]["fusion"]["components"]
                        components[0].update(weight=0.4, calibration=[[0, 0], [0.5, 0.4], [1, 1]])
                        components[1]["weight"] = 0.6
                        route, jobs = "configuration", "configuration-jobs"
                    response = await client.put(
                        f"/api/control/v1/{route}",
                        json=payload,
                        headers=headers,
                    )
                    assert response.status == 202
                    final = None
                    for _ in range(250):
                        for task in tasks:
                            if task.done():
                                await task
                        response = await client.get(
                            f"/api/control/v1/{jobs}/live-reload", headers=headers
                        )
                        final = await response.json()
                        active = [
                            b
                            for b in received
                            if b["message_type"] == "lidar_measurement"
                            and b["delivery_kind"] == "PERIODIC"
                            and b["config_revision"] == "cal-update-1"
                            and all(s["scan"] is not None for s in b["sensors"])
                        ]
                        estimates = [b for b in received if b.get("fill_estimate") is not None]
                        if (
                            final["state"] == "APPLIED"
                            and active
                            and (not all_settings or estimates)
                        ):
                            break
                        await asyncio.sleep(0.05)
                    assert final["state"] == "APPLIED"
                    assert active
                    sensors = {s["sensor_id"]: s for s in active[-1]["sensors"]}
                    assert sensors["lidar-a"]["scan"]["points"][0]["position_mm"]["z"] == 1000
                    if all_settings:
                        assert sensors["lidar-b"]["scan"]["points"][0]["position_mm"]["z"] == 1000
                        assert estimates[0]["fill_estimate"]["ratio"] == pytest.approx(
                            0.46, abs=0.001
                        )
                        assert estimates[0]["decision_state"] == "LIMITED"
                        assert "DEFAULT_SETTINGS" in estimates[0]["reason_codes"]
                    else:
                        assert sensors["lidar-b"]["scan"]["points"][0]["position_mm"] is None
                    registered = [b for b in received if b["message_type"] == "edge_configuration"]
                    assert any(b["config_revision"] == "cal-update-1" for b in registered)
                    local_status = json.loads(
                        (tmp_path / "status/lidar-processing.json").read_text()
                    )
                    assert local_status["configuration_transition_scans"] >= 1
        finally:
            stop.set()
            await asyncio.gather(*tasks)
            for server in servers:
                await server.stop(0)
