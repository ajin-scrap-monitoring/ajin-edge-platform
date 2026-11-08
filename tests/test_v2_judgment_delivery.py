import asyncio

import grpc
import httpx
import pytest
from ajin_edge.v2 import runtime
from ajin_edge.v2.ipc import DeliveryStub
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.outbox import CapacityError, Outbox
from ajin_edge.v2.processing import canonical, envelope, utc_now
from ajin_edge.v2.runtime import PendingSender
from test_v2_contracts import message
from test_v2_judgment import feed, judgment_configuration
from test_v2_processing import configuration


class Status:
    def __init__(self):
        self.reason = ""
        self.losses = 0

    def lost(self, reason):
        self.reason = reason
        self.losses += 1


async def test_judgment_bundle_sends_all_evidence_before_assessment():
    received = []

    class Client:
        async def enqueue(self, body):
            received.append(body["id"])

        async def enqueue_bundle(self, bodies):
            received.extend(body["id"] for body in bodies)

    sender = PendingSender(Client(), Status(), capacity=1)
    sender.offer([{"id": "scan-a"}, {"id": "scan-b"}, {"id": "assessment"}])
    task = asyncio.create_task(sender.run())
    try:
        async with asyncio.timeout(1):
            while len(received) < 3:
                await asyncio.sleep(0.001)
        assert received == ["scan-a", "scan-b", "assessment"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_evidence_local_rejection_does_not_send_dependent_assessment():
    received = []

    class Client:
        async def enqueue(self, body):
            received.append(body["id"])
            raise grpc.aio.AioRpcError(grpc.StatusCode.INVALID_ARGUMENT, None, None, "bad evidence")

        async def enqueue_bundle(self, bodies):
            await self.enqueue(bodies[0])

    status = Status()
    sender = PendingSender(Client(), status, capacity=1)
    sender.offer([{"id": "scan"}, {"id": "assessment"}])
    task = asyncio.create_task(sender.run())
    try:
        await asyncio.sleep(0.05)
        assert received == ["scan"]
        assert not sender.queue
        assert status.losses == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_queue_eviction_reports_whole_remaining_bundle_and_stays_bounded():
    status = Status()
    sender = PendingSender(None, status, capacity=1)
    sender.offer([{"id": "a"}, {"id": "b"}, {"id": "assessment-1"}])
    sender.offer([{"id": "assessment-2"}])
    assert len(sender.queue) == 1 and status.losses == 3
    assert status.reason == "LOCAL_DELIVERY_BACKLOG_LOSS"
    with pytest.raises(ValueError):
        sender.offer([])


async def test_uplink_drains_evidence_burst_without_fixed_ten_request_per_second_ceiling(
    tmp_path, monkeypatch
):
    import socket

    received = []

    async def backend(request):
        import json

        body = json.loads(request.content)
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

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        runtime.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(backend), **kwargs),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"127.0.0.1:{sock.getsockname()[1]}"
    cfg = configuration()
    (tmp_path / "token").write_text("test-token")
    config = {
        "configuration": {**cfg, **envelope(cfg, "edge_configuration")},
        "token_file": str(tmp_path / "token"),
        "backend_origin": "https://backend.example",
        "storage_dir": str(tmp_path / "outbox"),
        "status_dir": str(tmp_path / "status"),
        "delivery_endpoint": endpoint,
    }
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.uplink(config, stop))
    try:
        async with grpc.aio.insecure_channel(endpoint) as channel:
            await asyncio.wait_for(channel.channel_ready(), 3)
            client = DeliveryStub(channel)
            async with asyncio.timeout(3):
                for _ in range(50):
                    await client.enqueue(message())
                while len(received) < 50:
                    await asyncio.sleep(0.005)
            assert (await client.stats())["quarantined_messages"] == 0
    finally:
        stop.set()
        await task


def judgment_bundle():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    return orch.assessment_bundle(now_ns=1000000000)


@pytest.mark.parametrize("limit_kib", [40, 48])
def test_spool_cannot_accept_judgment_after_evicting_its_own_evidence(tmp_path, limit_kib):
    bundle = judgment_bundle()
    with Outbox(tmp_path, limit_bytes=limit_kib * 1024, reserve_bytes=16384) as store:
        try:
            store.enqueue_bundle(bundle)
        except CapacityError:
            assert store.inspect(bundle[-1]["message_id"])["state"] == "unknown"
        else:
            # Empty lock files and physical allocation differ between filesystems.
            # Admission is valid only if every input remains, with no hidden eviction.
            assert all(store.get(body["message_id"]) is not None for body in bundle)
            assert store.stats()["lost_messages"] == 0


def test_durable_bundle_preserves_all_evidence_and_is_idempotent(tmp_path):
    bundle = judgment_bundle()
    with Outbox(tmp_path) as store:
        assert store.enqueue_bundle(bundle) is False
        assert store.enqueue_bundle(bundle) is True
        assert all(store.get(body["message_id"]) is not None for body in bundle)
        assert store.stats()["lost_messages"] == 0
        assert store.meta["leases"] == {}


def test_malformed_bundle_is_rejected_before_any_write(tmp_path):
    bundle = judgment_bundle()
    bundle[-1]["evidence"]["scans"][0]["scan_id"] = "unrelated-scan"
    with Outbox(tmp_path) as store:
        with pytest.raises(ValueError):
            store.enqueue_bundle(bundle)
        assert not store.index


async def test_bundle_transport_keeps_each_scan_in_its_own_rpc():
    calls = []

    class SmallTransport(DeliveryStub):
        async def _call(self, method, value):
            calls.append((method, len(canonical(value))))
            if len(canonical(value)) > 8192:
                raise grpc.aio.AioRpcError(
                    grpc.StatusCode.RESOURCE_EXHAUSTED, None, None, "too large"
                )
            return {"state": "pending"}

    bundle = judgment_bundle()
    assert len(canonical(bundle)) > 8192
    await SmallTransport(None).enqueue_bundle(bundle)
    assert max(n for _, n in calls) <= 8192
    assert sum(method == "Enqueue" for method, _ in calls) == len(bundle) - 1


def test_staged_evidence_survives_ack_before_assessment_commit(tmp_path):
    import hashlib

    bundle = judgment_bundle()
    manifest = [
        {"message_id": b["message_id"], "digest": hashlib.sha256(canonical(b)).hexdigest()}
        for b in bundle[:-1]
    ]
    with Outbox(tmp_path) as store:
        store.begin_bundle(bundle[-1], manifest)
        for b in bundle[:-1]:
            store.enqueue(b)
            store.delivered(b["message_id"], "2026-09-20T00:00:01Z")
            assert store.get(b["message_id"]) is not None
        store.commit_bundle(bundle[-1])
        assert all(store.get(b["message_id"]) is not None for b in bundle)
        assert not store.meta["leases"]
