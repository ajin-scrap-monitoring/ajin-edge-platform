"""Explicit v2 service entry point. Existing v1 commands remain unchanged."""

import argparse
import asyncio
import json
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import grpc
import httpx

from ajin_edge.lifecycle import Watchdog, supervise, termination_event
from ajin_edge.status import atomic_json
from ajin_edge.transport import read_token
from ajin_edge.wire import lidar_pb2, lidar_pb2_grpc

from .adapter import from_frame
from .contracts import validate_message
from .ipc import OBSERVATIONS, OPTIONS, DeliveryStub, ObservationBroker, serve_delivery
from .orchestration import ConfigurationGate, Orchestrator
from .outbox import CapacityError, Outbox
from .processing import ProcessingEngine, canonical, utc_now
from .transport import DeliveryClient
from .worker import DeliveryWorker

STATUS_RESERVE = 1024 * 1024


def status_bytes(directory):
    root = Path(directory)
    if not root.exists():
        return 0
    total = 0
    for path in root.iterdir():
        try:
            stat = path.stat()
            if path.is_file():
                total += max(
                    ((stat.st_size + 4095) // 4096) * 4096, getattr(stat, "st_blocks", 0) * 512
                )
        except FileNotFoundError:
            continue  # Another producer completed an atomic replacement.
    return total


def load_runtime(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    registration = config["configuration"]
    validate_message(registration)
    if registration["message_type"] != "edge_configuration":
        raise ValueError("configuration registration required")
    expected = {s["sensor_id"] for s in registration["sensors"]}
    if set(config["drivers"]) != expected:
        raise ValueError("driver endpoints must match configured sensors")
    endpoints = [
        config["delivery_endpoint"],
        config["observation_endpoint"],
        *config["drivers"].values(),
    ]
    if len(set(endpoints)) != len(endpoints) or any(not e.startswith("unix:/") for e in endpoints):
        raise ValueError("distinct private Unix domain sockets required")
    for key in ("storage_dir", "status_dir", "token_file", "clock_domain_file"):
        if not Path(config[key]).is_absolute():
            raise ValueError(f"{key} must be absolute")
    storage, statuses = Path(config["storage_dir"]).resolve(), Path(config["status_dir"]).resolve()
    if storage == statuses or storage in statuses.parents or statuses in storage.parents:
        raise ValueError("outbox and status directories must be separate siblings")
    # Validate origin without exposing token or making any requests.
    DeliveryClient(config["backend_origin"], "validation-only", None)
    return config


class LocalStatus:
    def __init__(self, config, service):
        self.config, self.service = config, service
        self.progress = None
        self.losses = 0
        self.reason = ""

    def advanced(self):
        self.progress = utc_now()

    def write(self, state="HEALTHY", **telemetry):
        registration = self.config["configuration"]
        # Each of the three v2 writers has one bounded snapshot and one replacement.
        if (
            len(canonical(telemetry)) > 60000
            or status_bytes(self.config["status_dir"]) > STATUS_RESERVE - 65536
        ):
            raise CapacityError("bounded status storage exhausted")
        atomic_json(
            Path(self.config["status_dir"]) / f"{self.service}.json",
            {
                "schema_version": "2.0",
                "service": self.service,
                "site_id": registration["site_id"],
                "edge_id": registration["edge_id"],
                "config_revision": registration["config_revision"],
                "reported_at": utc_now(),
                "last_progress_at": self.progress,
                "state": state,
                "reason_codes": [self.reason] if self.reason else [],
                "local_dropped_messages": self.losses,
                **telemetry,
            },
        )


class PendingSender:
    def __init__(self, client, status, *, capacity=10):
        self.client, self.status, self.capacity = client, status, capacity
        self.queue = deque()

    def offer(self, payload):
        if len(self.queue) >= self.capacity:
            self.queue.popleft()
            self.status.losses += 1
            self.status.reason = "LOCAL_DELIVERY_BACKLOG_LOSS"
        self.queue.append(payload)

    async def run(self):
        while True:
            if self.queue:
                payload = self.queue[0]
                try:
                    await self.client.enqueue(payload)
                    if self.status.reason == "LOCAL_DELIVERY_UNAVAILABLE":
                        self.status.reason = ""
                    if self.queue and self.queue[0] is payload:
                        self.queue.popleft()
                except grpc.aio.AioRpcError as error:
                    self.status.reason = "LOCAL_DELIVERY_UNAVAILABLE"
                    if error.code() in (
                        grpc.StatusCode.INVALID_ARGUMENT,
                        grpc.StatusCode.ALREADY_EXISTS,
                    ):
                        # Do not spin indefinitely on a locally rejected body.
                        if self.queue and self.queue[0] is payload:
                            self.queue.popleft()
                            self.status.losses += 1
            await asyncio.sleep(0.1)


async def register(client, registration, gate, status):
    gate.propose(registration)
    while gate.active is None:
        try:
            await client.enqueue(registration)
            reply = await client.inspect(registration["message_id"])
            if reply["remote_acked"]:
                gate.acknowledge(registration["message_id"])
                status.reason = ""
                return
        except grpc.aio.AioRpcError:
            pass
        status.reason = "CONFIGURATION_ACK_PENDING"
        status.write("STARTING")
        await asyncio.sleep(1)


async def processing(config, stop):
    registration = config["configuration"]
    domain = Path(config["clock_domain_file"]).read_text().strip()
    engine = ProcessingEngine(registration, clock_domain_id=domain)
    gate, broker = ConfigurationGate(), ObservationBroker()
    status = LocalStatus(config, "lidar-processing")
    server, _ = await broker.serve(config["observation_endpoint"])
    async with grpc.aio.insecure_channel(config["delivery_endpoint"], options=OPTIONS) as channel:
        client = DeliveryStub(channel)
        sender = PendingSender(client, status)

        async def consume(sensor_id, endpoint):
            while True:
                try:
                    async with grpc.aio.insecure_channel(endpoint, options=OPTIONS) as source:
                        stub = lidar_pb2_grpc.LidarScanSourceStub(source)
                        async for frame in stub.SubscribeScans(
                            lidar_pb2.SubscribeRequest(consumer_id="processing-v2")
                        ):
                            try:
                                raw = from_frame(
                                    frame,
                                    edge_id=registration["edge_id"],
                                    sensor_id=sensor_id,
                                    revision=registration["config_revision"],
                                )
                                observation = engine.ingest(raw)
                                status.advanced()
                                if status.reason in ("DRIVER_UNAVAILABLE", "SCAN_REJECTED"):
                                    status.reason = ""
                                if observation and gate.active is not None:
                                    broker.publish(
                                        {
                                            "config_revision": registration["config_revision"],
                                            "sensor": observation,
                                        }
                                    )
                            except ValueError:
                                status.reason = "SCAN_REJECTED"
                                status.losses += 1
                except grpc.aio.AioRpcError:
                    status.reason = "DRIVER_UNAVAILABLE"
                await asyncio.sleep(1)

        async def periodic():
            await register(client, registration, gate, status)
            while True:
                start = time.monotonic()
                sender.offer(engine.periodic(now_ns=time.monotonic_ns()))
                status.write(
                    "DEGRADED" if status.reason else "HEALTHY",
                    dropped_scans=engine.dropped_scans,
                    dropped_observations=broker.dropped,
                )
                await asyncio.sleep(max(0, 1 - (time.monotonic() - start)))

        try:
            with Watchdog() as watchdog:
                await supervise(
                    stop,
                    periodic(),
                    sender.run(),
                    watchdog.pulse(),
                    *(consume(sensor, endpoint) for sensor, endpoint in config["drivers"].items()),
                )
        finally:
            await server.stop(3)


async def uplink(config, stop):
    registration = config["configuration"]
    status = LocalStatus(config, "measurement-uplink")
    latest_status = {}
    with Outbox(
        config["storage_dir"],
        external_reserve_bytes=STATUS_RESERVE,
        external_usage=lambda: status_bytes(config["status_dir"]),
    ) as store:
        server, _ = await serve_delivery(
            store,
            config["delivery_endpoint"],
            site_id=registration["site_id"],
            edge_id=registration["edge_id"],
            latest_status=latest_status,
        )
        async with httpx.AsyncClient(trust_env=False) as http:
            transport = DeliveryClient(
                config["backend_origin"], read_token(config["token_file"]), http
            )
            worker = DeliveryWorker(store, transport, latest_status=latest_status)

            async def deliver():
                while True:
                    # Rotation is detected without removing or reopening queued records.
                    try:
                        token = read_token(config["token_file"])
                    except (OSError, ValueError):
                        status.reason = "TOKEN_UNAVAILABLE"
                        await asyncio.sleep(1)
                        continue
                    transport.token = token
                    try:
                        await worker.step()
                    except (CapacityError, OSError):
                        worker.reason = "STORAGE_UNAVAILABLE"
                    status.progress = worker.last_progress_at
                    status.reason = worker.reason
                    await asyncio.sleep(0.1)

            async def report():
                while True:
                    await store.call("collect")
                    state = (
                        "FATAL"
                        if worker.blocked_token or worker.configuration_blocked
                        else ("RETRYING" if worker.reason else "HEALTHY")
                    )
                    status.write(state, **await store.call("stats"))
                    await asyncio.sleep(2)

            try:
                with Watchdog() as watchdog:
                    await supervise(stop, deliver(), report(), watchdog.pulse())
            finally:
                await server.stop(3)


def service_snapshots(config):
    expected = [
        "lidar-driver-" + (s[6:] if s.startswith("lidar-") else s) for s in config["drivers"]
    ] + ["lidar-processing", "measurement-uplink", "camera-edge", "edge-orchestrator"]
    result = []
    registration = config["configuration"]
    for name in expected:
        item = {
            "service": name,
            "state": "UNKNOWN",
            "last_progress_at": None,
            "reason_codes": ["STATUS_UNAVAILABLE"],
        }
        path = Path(config["status_dir"]) / f"{name}.json"
        try:
            if path.stat().st_size > 65536:
                raise ValueError("oversized status")
            source = json.loads(path.read_bytes())
            reported = datetime.fromisoformat(source["reported_at"])
            age = (datetime.now(UTC) - reported).total_seconds()
            if (
                source["edge_id"] != registration["edge_id"]
                or source["site_id"] != registration["site_id"]
                or source["config_revision"] != registration["config_revision"]
                or not 0 <= age <= 15
            ):
                raise ValueError("stale or foreign status")
            item.update(
                state=source["state"],
                last_progress_at=source.get("last_progress_at"),
                reason_codes=source["reason_codes"],
            )
            losses = any(
                source.get(key, 0) > 0
                for key in (
                    "dropped_scans",
                    "dropped_observations",
                    "local_dropped_messages",
                    "frame_loss",
                )
            )
            if losses:
                item["reason_codes"] = sorted(set(item["reason_codes"]) | {"OBSERVATION_LOSS"})
                if item["state"] == "HEALTHY":
                    item["state"] = "DEGRADED"
        except (OSError, ValueError, KeyError, TypeError):
            pass
        result.append(item)
    return result


async def orchestrator(config, stop):
    registration = config["configuration"]
    orch, gate = Orchestrator(registration), ConfigurationGate()
    status = LocalStatus(config, "edge-orchestrator")
    async with grpc.aio.insecure_channel(config["delivery_endpoint"], options=OPTIONS) as channel:
        client = DeliveryStub(channel)
        sender = PendingSender(client, status)
        configured = asyncio.Event()

        async def consume():
            while True:
                try:
                    async with grpc.aio.insecure_channel(
                        config["observation_endpoint"], options=OPTIONS
                    ) as source:
                        async for item in source.unary_stream(
                            OBSERVATIONS,
                            request_serializer=canonical,
                            response_deserializer=json.loads,
                        )({}):
                            observation = item["observation"]
                            if observation["config_revision"] != registration["config_revision"]:
                                status.reason = "OBSERVATION_CONFIG_MISMATCH"
                                continue
                            if orch.ingest(observation["sensor"], now_ns=time.monotonic_ns()):
                                status.advanced()
                                if status.reason == "OBSERVATIONS_UNAVAILABLE":
                                    status.reason = ""
                            if item["dropped_observations"]:
                                status.reason = "OBSERVATION_LOSS"
                except grpc.aio.AioRpcError:
                    status.reason = "OBSERVATIONS_UNAVAILABLE"
                await asyncio.sleep(1)

        async def periodic():
            await register(client, registration, gate, status)
            configured.set()
            last_status, fingerprint = -10.0, None
            while True:
                start = time.monotonic()
                try:
                    stats = await client.stats()
                    snapshot = orch.status(
                        stats, service_snapshots(config), now_ns=time.monotonic_ns()
                    )
                    signature = canonical(
                        {
                            "overall_state": snapshot["overall_state"],
                            "reason_codes": snapshot["reason_codes"],
                            "sensors": [
                                (s["sensor_id"], s["data_state"]) for s in snapshot["sensors"]
                            ],
                            "services": [
                                (s["service"], s["state"], s["reason_codes"])
                                for s in snapshot["services"]
                            ],
                        }
                    )
                    if signature != fingerprint or start - last_status >= 10:
                        await client.enqueue(snapshot)
                        last_status, fingerprint = start, signature
                except grpc.aio.AioRpcError:
                    status.reason = "LOCAL_DELIVERY_UNAVAILABLE"
                status.write(
                    "DEGRADED" if status.reason else "HEALTHY", observations=orch.observations
                )
                await asyncio.sleep(max(0, 1 - (time.monotonic() - start)))

        async def judgments():
            await configured.wait()
            while True:
                start = time.monotonic()
                sender.offer(orch.assessment())
                await asyncio.sleep(max(0, 1 - (time.monotonic() - start)))

        with Watchdog() as watchdog:
            await supervise(
                stop, consume(), periodic(), judgments(), sender.run(), watchdog.pulse()
            )


def main():
    parser = argparse.ArgumentParser(description="Explicit v2 edge services; does not migrate v1")
    parser.add_argument("role", choices=("processing", "uplink", "orchestrator"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_runtime(args.config)
    os.umask(0o007)

    async def run():
        await {"processing": processing, "uplink": uplink, "orchestrator": orchestrator}[args.role](
            config, termination_event()
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
