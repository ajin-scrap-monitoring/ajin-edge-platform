import asyncio
from copy import deepcopy

from ajin_edge.v2.outbox import Outbox
from ajin_edge.v2.runtime import LocalStatus, service_snapshots
from ajin_edge.v2.transport import Result
from ajin_edge.v2.worker import DeliveryWorker
from test_v2_contracts import message
from test_v2_processing import configuration


def test_camera_revision_is_independent_of_lidar_calibration(tmp_path):
    cfg = {
        "configuration": configuration(),
        "status_dir": str(tmp_path),
        "drivers": {},
        "camera_config_revision": "camera-v1",
    }
    camera = deepcopy(cfg)
    camera["configuration"]["config_revision"] = "camera-v1"
    LocalStatus(camera, "camera-edge").write()
    result = {s["service"]: s for s in service_snapshots(cfg)}
    assert result["camera-edge"]["state"] == "HEALTHY"
    camera["configuration"]["config_revision"] = "foreign"
    LocalStatus(camera, "camera-edge").write()
    assert (
        next(s for s in service_snapshots(cfg) if s["service"] == "camera-edge")["state"]
        == "UNKNOWN"
    )


async def test_periodic_waits_for_earlier_ack_and_preserves_fifo(tmp_path):
    bodies = [message() for _ in range(3)]
    release = asyncio.Event()
    entered = asyncio.Event()
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            entered.set()
            if body["message_id"] == bodies[0]["message_id"]:
                await release.wait()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        worker = DeliveryWorker(store, Transport())
        task = asyncio.create_task(worker.step())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0.02)
            assert sent == [bodies[0]["message_id"]]
        finally:
            release.set()
            await task
        for _ in range(3):
            await worker.step()
        assert sent == [b["message_id"] for b in bodies]


async def test_retry_blocks_newer_periodic_and_evidence_still_runs(tmp_path):
    from test_v2_delivery_batch import message as evidence

    bodies = [message(), message()]
    other = evidence()
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            if body["message_id"] == bodies[0]["message_id"]:
                return Result("retry", "NETWORK_OR_TIMEOUT")
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in [*bodies, other]:
            store.enqueue(body)
        worker = DeliveryWorker(store, Transport())
        await worker.step(now=0)
        assert bodies[1]["message_id"] not in sent
        assert other["message_id"] in sent
        assert store.get(bodies[0]["message_id"])["state"] == "pending"
        await worker.step(now=worker.next_attempt + 1)
        assert sent.count(bodies[0]["message_id"]) == 2
        assert bodies[1]["message_id"] not in sent


async def test_calibration_keeps_camera_revision(tmp_path):
    from ajin_edge.v2.configuration_state import ControlState, effective_runtime
    from test_v2_calibration_control import Delivery, base_runtime, calibration_request

    base = base_runtime(tmp_path)
    with ControlState(base) as state:
        request = calibration_request(base)
        state.propose(request, "camera-independent")
        delivery = Delivery()
        delivery.acked = True
        await state.advance(delivery)
        active = effective_runtime(base)
        assert active["configuration"]["config_revision"] == request["config_revision"]
        assert active["camera_config_revision"] == base["configuration"]["config_revision"]


async def test_periodic_attempt_trace_is_bounded_and_contains_no_points(monkeypatch):
    from ajin_edge.v2.transport import DeliveryClient

    client = DeliveryClient("https://backend.example", "test", None)

    async def respond(_):
        return Result("accepted", received_at="2026-09-20T01:00:00Z")

    monkeypatch.setattr(client, "_send", respond)
    for _ in range(20):
        body = message()
        client.record_registration(body["message_id"], 1000000000)
        await client.send(body)
    trace = client.metrics["PERIODIC"]["recent_attempts"]
    assert len(trace) == 16
    assert trace[-1]["message_id"] == body["message_id"]
    assert trace[-1]["registered_at"] == "1970-01-01T00:00:01+00:00"
    assert "sensors" not in trace[-1]
    assert not client.registrations


def test_state_backlog_does_not_starve_old_evidence(tmp_path):
    from ajin_edge.v2.orchestration import Orchestrator
    from test_v2_delivery_batch import message as evidence
    from test_v2_judgment import feed, judgment_configuration

    orch = Orchestrator(judgment_configuration())
    feed(orch, judgment_configuration())
    with Outbox(tmp_path) as store:
        old = evidence()
        store.enqueue(old)
        for _ in range(20):
            bundle = orch.assessment_bundle(now_ns=1000000000)
            store.enqueue(bundle[-1])
        rows = store.next_ordered_many()
        assert old["message_id"] in [r["payload"]["message_id"] for r in rows]


async def test_next_periodic_does_not_wait_for_slow_evidence_ack(tmp_path):
    from test_v2_delivery_batch import message as evidence

    bodies = [message(), message()]
    other = evidence()
    release, second = asyncio.Event(), asyncio.Event()

    class Transport:
        token = "test"

        async def send(self, body):
            if body["message_id"] == other["message_id"]:
                await release.wait()
            if body["message_id"] == bodies[1]["message_id"]:
                second.set()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in [*bodies, other]:
            store.enqueue(body)
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step())
        try:
            await asyncio.wait_for(second.wait(), 0.5)
        finally:
            release.set()
            await task


async def test_periodic_flush_survives_an_already_running_ack_write(tmp_path, monkeypatch):
    import threading

    from test_v2_delivery_batch import message as evidence

    bodies = [message(), message()]
    entered, release_write = threading.Event(), threading.Event()
    release_http, periodic_done, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    evidence_calls = 0

    class Transport:
        token = "test"

        async def send(self, body):
            nonlocal evidence_calls
            if body["message_id"] == bodies[0]["message_id"]:
                await asyncio.to_thread(entered.wait, 2)
                periodic_done.set()
            elif body["message_id"] == bodies[1]["message_id"]:
                second.set()
            else:
                evidence_calls += 1
                if evidence_calls > 8:
                    await release_http.wait()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in [*bodies, *[evidence() for _ in range(16)]]:
            store.enqueue(body)
        original = store.delivered_many

        def gated(receipts, **kwargs):
            if not entered.is_set():
                entered.set()
                release_write.wait(3)
            return original(receipts, **kwargs)

        monkeypatch.setattr(store, "delivered_many", gated)
        task = asyncio.create_task(DeliveryWorker(store, Transport(), clock=lambda: 0).step())
        try:
            await asyncio.wait_for(periodic_done.wait(), 1)
            await asyncio.sleep(0.03)
            release_write.set()
            await asyncio.wait_for(second.wait(), 0.5)
        finally:
            release_write.set()
            release_http.set()
            await task


async def test_periodic_refills_through_full_reserved_evidence_batch(tmp_path):
    from test_v2_delivery_batch import message as evidence

    bodies = [message(), message()]
    release, second = asyncio.Event(), asyncio.Event()

    class Transport:
        token = "test"

        async def send(self, body):
            if body.get("delivery_kind") == "EVIDENCE":
                await release.wait()
            elif body["message_id"] == bodies[1]["message_id"]:
                second.set()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in [*bodies, *[evidence() for _ in range(20)]]:
            store.enqueue(body)
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step())
        try:
            await asyncio.wait_for(second.wait(), 0.5)
            assert len(store.inflight) <= 16
        finally:
            release.set()
            await task
