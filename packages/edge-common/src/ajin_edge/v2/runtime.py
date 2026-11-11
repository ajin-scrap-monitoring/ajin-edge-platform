"""Explicit v2 service entry point. Existing v1 commands remain unchanged."""

import argparse
import asyncio
import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import grpc
import httpx

from ajin_edge.lifecycle import Watchdog, supervise, termination_event
from ajin_edge.status import atomic_json
from ajin_edge.transport import read_token
from ajin_edge.wire import lidar_pb2, lidar_pb2_grpc

from .adapter import from_frame
from .auxiliary_storage import STATUS_RESERVE, auxiliary_bytes
from .configuration_state import control_path, effective_runtime
from .contracts import validate_message
from .ipc import OBSERVATIONS, OPTIONS, DeliveryStub, ObservationBroker, serve_delivery
from .loss_recovery import OutboxLossRecovery
from .orchestration import ConfigurationGate, Orchestrator
from .outbox import CapacityError, Outbox
from .processing import ProcessingEngine, canonical, identifier, utc_now
from .transport import DeliveryClient
from .worker import DeliveryWorker


def load_runtime(path, *, apply_control=True):
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
    if "driver_config_revision" in config:
        identifier(config["driver_config_revision"])
    if "configuration_state_file" in config:
        identifier(config["driver_config_revision"])
        control_path(config)
        if apply_control:
            config = effective_runtime(config)
    return config


class LocalStatus:
    def __init__(self, config, service):
        self.config, self.service = config, service
        self.progress = None
        self.losses = 0
        self.reason = ""
        self.instance_id = str(uuid4())
        self.clock_domain_id = (
            Path(config["clock_domain_file"]).read_text().strip()
            if "clock_domain_file" in config
            else None
        )
        self.last_backlog_loss = None

    def lost(self, reason):
        self.losses += 1
        self.reason = reason
        if reason == "LOCAL_DELIVERY_BACKLOG_LOSS":
            self.last_backlog_loss = time.monotonic()

    def advanced(self):
        self.progress = utc_now()

    def write(self, state="HEALTHY", **telemetry):
        if (
            self.reason == "LOCAL_DELIVERY_BACKLOG_LOSS"
            and self.last_backlog_loss is not None
            and time.monotonic() - self.last_backlog_loss >= LossTracker.recovery_seconds
        ):
            self.reason = ""
            if state == "DEGRADED":
                state = "HEALTHY"
        registration = self.config["configuration"]
        # Each of the three v2 writers has one bounded snapshot and one replacement.
        if (
            len(canonical(telemetry)) > 60000
            or auxiliary_bytes(self.config) > STATUS_RESERVE - 65536
        ):
            raise CapacityError("bounded status storage exhausted")
        atomic_json(
            Path(self.config["status_dir"]) / f"{self.service}.json",
            {
                "schema_version": "2.0",
                "service": self.service,
                "instance_id": self.instance_id,
                "clock_domain_id": self.clock_domain_id,
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
        self._space_available = asyncio.Event()

    async def wait_for_capacity(self):
        """Single producer: wait before calculating, retaining no extra completed bundle."""
        while len(self.queue) >= self.capacity:
            self._space_available.clear()
            await self._space_available.wait()

    def offer(self, payload):
        if isinstance(payload, list) and (not payload or len(payload) > 17):
            raise ValueError("judgment bundle must contain 1..17 messages")
        if len(self.queue) >= self.capacity:
            dropped = self.queue.popleft()
            self.status.lost("LOCAL_DELIVERY_BACKLOG_LOSS")
            if isinstance(dropped, list):
                self.status.losses += len(dropped) - 1
        self.queue.append(payload)

    async def run(self):
        pending = None
        while True:
            delay = 0.1
            if pending is None and self.queue:
                pending = self.queue.popleft()
                self._space_available.set()
            if pending is not None:
                try:
                    if isinstance(pending, list) and len(pending) > 1:
                        await self.client.enqueue_bundle(pending)
                    else:
                        await self.client.enqueue(
                            pending[0] if isinstance(pending, list) else pending
                        )
                    delay = 0
                    if self.status.reason == "LOCAL_DELIVERY_UNAVAILABLE":
                        self.status.reason = ""
                    pending = None
                except grpc.aio.AioRpcError as error:
                    self.status.reason = "LOCAL_DELIVERY_UNAVAILABLE"
                    if error.code() in (
                        grpc.StatusCode.INVALID_ARGUMENT,
                        grpc.StatusCode.ALREADY_EXISTS,
                    ):
                        # Do not spin indefinitely on a locally rejected body.
                        self.status.losses += len(pending) if isinstance(pending, list) else 1
                        pending = None
            await asyncio.sleep(delay if self.queue or pending is not None else 0.1)


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
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v2-scan-processing")
    source_revision = config.get("driver_config_revision", registration["config_revision"])
    cutoff_ns = time.monotonic_ns() if config.get("configuration_activation") else None
    transition_scans = 0

    def ingest_frame(frame, sensor_id):
        nonlocal transition_scans
        raw = from_frame(
            frame,
            edge_id=registration["edge_id"],
            sensor_id=sensor_id,
            revision=source_revision,
        )
        if cutoff_ns is not None and int(raw["acquired_monotonic_ns"]) < cutoff_ns:
            transition_scans += 1
            return None
        return engine.ingest(raw)

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
                                observation = await loop.run_in_executor(
                                    executor, ingest_frame, frame, sensor_id
                                )
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
                sender.offer(
                    await loop.run_in_executor(
                        executor, lambda: engine.periodic(now_ns=time.monotonic_ns())
                    )
                )
                status.write(
                    "DEGRADED" if status.reason else "HEALTHY",
                    dropped_scans=engine.dropped_scans,
                    dropped_observations=broker.dropped,
                    configuration_transition_scans=transition_scans,
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
            executor.shutdown(wait=True, cancel_futures=True)


async def uplink(config, stop):
    registration = config["configuration"]
    status = LocalStatus(config, "measurement-uplink")
    latest_status = {}
    with Outbox(
        config["storage_dir"],
        external_reserve_bytes=STATUS_RESERVE,
        external_usage=lambda: auxiliary_bytes(config),
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
                    progressed = False
                    try:
                        progressed = await worker.step()
                    except (CapacityError, OSError):
                        worker.reason = "STORAGE_UNAVAILABLE"
                    status.progress = worker.last_progress_at
                    status.reason = worker.reason
                    # EVIDENCE can arrive in bounded bursts above the 1Hz periodic feed.
                    # Backoff/auth pauses still return False and retain the idle delay.
                    await asyncio.sleep(0 if progressed else 0.1)

            async def report():
                while True:
                    await store.call("collect")
                    state = (
                        "FATAL"
                        if worker.blocked_token or worker.configuration_blocked
                        else ("RETRYING" if worker.reason else "HEALTHY")
                    )
                    status.write(
                        state,
                        http_delivery=transport.metrics,
                        **await store.call("stats", audit=False),
                    )
                    await asyncio.sleep(2)

            try:
                with Watchdog() as watchdog:
                    await supervise(stop, deliver(), report(), watchdog.pulse())
            finally:
                await server.stop(3)


class LossTracker:
    """Keep cumulative counters intact; alert for ten seconds since a new loss."""

    recovery_seconds = 10

    def __init__(self):
        self.sources = {}

    def observe(self, name, instance, counters, *, now):
        previous = self.sources.get(name)
        since = None
        if previous is not None and previous[0] == instance:
            old, since = previous[1:]
            if any(value < old.get(key, 0) for key, value in counters.items()):
                since = None
                increased = any(value > 0 for value in counters.values())
            else:
                increased = any(value > old.get(key, 0) for key, value in counters.items())
        else:
            increased = any(value > 0 for value in counters.values())
        if increased:
            since = now
        self.sources[name] = (instance, dict(counters), since)
        return since is not None and now - since < self.recovery_seconds


def service_snapshots(config, *, loss_tracker=None, now=None, identities=None):
    loss_tracker = loss_tracker if loss_tracker is not None else LossTracker()
    now = time.monotonic() if now is None else now
    expected = [
        "lidar-driver-" + (s[6:] if s.startswith("lidar-") else s) for s in config["drivers"]
    ] + ["lidar-processing", "measurement-uplink", "camera-edge", "edge-orchestrator"]
    result = []
    registration = config["configuration"]
    for name in expected:
        if identities is not None:
            identities[name] = None
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
            if identities is not None:
                identities[name] = source.get("instance_id")
            item.update(
                state=source["state"],
                last_progress_at=source.get("last_progress_at"),
                reason_codes=source["reason_codes"],
            )
            counters = {
                key: source.get(key, 0)
                for key in (
                    "dropped_scans",
                    "dropped_observations",
                    "local_dropped_messages",
                    "frame_loss",
                )
            }
            losses = loss_tracker.observe(name, source.get("instance_id"), counters, now=now)
            if losses:
                item["reason_codes"] = sorted(set(item["reason_codes"]) | {"OBSERVATION_LOSS"})
                if item["state"] == "HEALTHY":
                    item["state"] = "DEGRADED"
        except (OSError, ValueError, KeyError, TypeError):
            pass
        result.append(item)
    return result


def recovery_delivery_healthy(config, *, now=None):
    """Local proof of current raw/judgment delivery, not just old backlog ACKs."""
    now = datetime.now(UTC) if now is None else now
    try:
        path = Path(config["status_dir"]) / "measurement-uplink.json"
        if path.stat().st_size > 65536:
            return False
        source = json.loads(path.read_bytes())
        if source["state"] != "HEALTHY" or any(
            source[k] != config["configuration"][k]
            for k in ("site_id", "edge_id", "config_revision")
        ):
            return False
        stamps = [source["reported_at"]]
        for kind in ("PERIODIC", "STATE"):
            metric = source["http_delivery"][kind]
            stamps.extend(
                [metric["latest_accepted_generated_at"], metric["latest_accepted_observed_at"]]
            )
        for sensor in config["configuration"]["sensors"]:
            proof = source["http_delivery"]["PERIODIC"]["latest_accepted_sensors"][
                sensor["sensor_id"]
            ]
            stamps.extend([proof["generated_at"], proof["observed_at"], proof["acquired_at"]])
        return all(0 <= (now - datetime.fromisoformat(at)).total_seconds() <= 15 for at in stamps)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def previous_loss_high_water(config):
    # Carry the diagnostic high-water mark across restarts, never timer credit.
    try:
        path = Path(config["status_dir"]) / "edge-orchestrator.json"
        if path.stat().st_size > 65536:
            return 0
        source = json.loads(path.read_bytes())
        if any(source[k] != config["configuration"][k] for k in ("site_id", "edge_id")):
            return 0
        value = source.get("loss_recovery", {}).get("loss_high_water", 0)
        return value if type(value) is int and value >= 0 else 0
    except (OSError, ValueError, KeyError, TypeError):
        return 0


async def orchestrator(config, stop):
    registration = config["configuration"]
    orch, gate = Orchestrator(registration), ConfigurationGate()
    orch.loss_recovery = OutboxLossRecovery(previous_loss_high_water(config))
    status = LocalStatus(config, "edge-orchestrator")
    service_losses, observation_losses = LossTracker(), LossTracker()
    observation_count = 0
    observation_instance = None
    async with grpc.aio.insecure_channel(config["delivery_endpoint"], options=OPTIONS) as channel:
        client = DeliveryStub(channel)
        # One pending judgment bundle, bounded at 2*8 evidence scans plus its assessment.
        sender = PendingSender(client, status, capacity=1)
        configured = asyncio.Event()

        async def consume():
            nonlocal observation_count, observation_instance
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
                            observation_count = item["dropped_observations"]
                            observation_instance = item.get("stream_instance_id")
                            if observation_losses.observe(
                                "observations",
                                observation_instance,
                                {"dropped_observations": observation_count},
                                now=time.monotonic(),
                            ):
                                status.reason = "OBSERVATION_LOSS"
                            elif status.reason == "OBSERVATION_LOSS":
                                status.reason = ""
                except grpc.aio.AioRpcError:
                    status.reason = "OBSERVATIONS_UNAVAILABLE"
                await asyncio.sleep(1)

        async def periodic():
            await register(client, registration, gate, status)
            configured.set()
            last_status, fingerprint = -10.0, None
            while True:
                start = time.monotonic()
                if status.reason == "OBSERVATION_LOSS" and not observation_losses.observe(
                    "observations",
                    observation_instance,
                    {"dropped_observations": observation_count},
                    now=start,
                ):
                    status.reason = ""
                try:
                    stats = await client.stats()
                    identities = {}
                    services = service_snapshots(
                        config, loss_tracker=service_losses, now=start, identities=identities
                    )
                    snapshot = orch.status(
                        stats,
                        services,
                        now_ns=time.monotonic_ns(),
                        recovery_context={
                            "healthy": recovery_delivery_healthy(config)
                            and all(v for k, v in identities.items() if k != "camera-edge"),
                            "epoch": tuple(
                                sorted((k, v) for k, v in identities.items() if k != "camera-edge")
                            ),
                        },
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
                    orch.loss_recovery.interrupt()
                status.write(
                    "DEGRADED" if status.reason else "HEALTHY",
                    observations=orch.observations,
                    loss_recovery=orch.loss_recovery.snapshot(time.monotonic()),
                )
                await asyncio.sleep(max(0, 1 - (time.monotonic() - start)))

        async def judgments():
            await configured.wait()
            while True:
                # Build from fresh observations only after the bounded pending
                # slot is free; a slow durable write must not evict a judgment.
                await sender.wait_for_capacity()
                start = time.monotonic()
                sender.offer(await asyncio.to_thread(orch.assessment_bundle))
                await asyncio.sleep(max(0, 1 - (time.monotonic() - start)))

        with Watchdog() as watchdog:
            await supervise(
                stop, consume(), periodic(), judgments(), sender.run(), watchdog.pulse()
            )


def configuration_stamps(path, config):
    paths = [Path(path)]
    if "configuration_state_file" in config:
        paths.append(Path(config["configuration_state_file"]))
    return tuple((s.st_mtime_ns, s.st_size, s.st_ino) for s in (p.stat() for p in paths))


def load_generation(path):
    config = load_runtime(path)
    for _ in range(10):
        before = configuration_stamps(path, config)
        candidate = load_runtime(path)
        after = configuration_stamps(path, candidate)
        if before == after:
            return candidate, after
        config = candidate
    raise ValueError("configuration changes too rapidly")


async def run_service(role, path, stop):
    """Recreate one service generation on committed configuration changes only."""
    stopped = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            config, stamps = load_generation(path)
            fingerprint = canonical(config)
            generation_stop = asyncio.Event()
            worker = asyncio.create_task(
                {"processing": processing, "uplink": uplink, "orchestrator": orchestrator}[role](
                    config, generation_stop
                )
            )
            failure = None
            try:
                while not stop.is_set():
                    done, _ = await asyncio.wait(
                        (worker, stopped), timeout=0.2, return_when=asyncio.FIRST_COMPLETED
                    )
                    if worker in done:
                        await worker
                        if not stop.is_set():
                            raise RuntimeError("service generation stopped unexpectedly")
                        return
                    if stopped in done:
                        return
                    try:
                        current = configuration_stamps(path, config)
                        if current == stamps:
                            continue
                        candidate, stamps = await asyncio.to_thread(load_generation, path)
                        if canonical(candidate) != fingerprint:
                            break
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        failure = error
                        break
            finally:
                generation_stop.set()
                await asyncio.wait_for(worker, 10)
            if failure is not None:
                status = LocalStatus(
                    config,
                    {
                        "processing": "lidar-processing",
                        "uplink": "measurement-uplink",
                        "orchestrator": "edge-orchestrator",
                    }[role],
                )
                status.reason = "CONFIGURATION_STATE_INVALID"
                status.write("FATAL")
                raise ValueError("configuration state unavailable") from failure
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Explicit v2 edge services; does not migrate v1")
    parser.add_argument("role", choices=("processing", "uplink", "orchestrator", "calibration-api"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_runtime(args.config, apply_control=args.role != "calibration-api")
    os.umask(0o007)

    async def run():
        if args.role == "calibration-api":
            from .calibration_api import run as control_api

            await control_api(config, termination_event())
        else:
            await run_service(args.role, args.config, termination_event())

    asyncio.run(run())


if __name__ == "__main__":
    main()
