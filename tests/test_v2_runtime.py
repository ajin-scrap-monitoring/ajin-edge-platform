import grpc
import pytest
from ajin_edge.v2.ipc import DeliveryStub, ObservationBroker, serve_delivery
from ajin_edge.v2.outbox import CapacityError, Outbox
from ajin_edge.v2.transport import Result
from ajin_edge.v2.worker import DeliveryWorker
from test_v2_contracts import message


async def test_local_enqueue_is_not_remote_ack(tmp_path):
    with Outbox(tmp_path) as store:
        body = message()
        server, port = await serve_delivery(
            store, "127.0.0.1:0", site_id=body["site_id"], edge_id=body["edge_id"]
        )
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                client = DeliveryStub(channel)
                reply = await client.enqueue(body)
                assert reply["state"] == "pending"
                assert (await client.inspect(body["message_id"]))["remote_acked"] is False
                store.delivered(body["message_id"], "2026-09-20T01:00:00Z")
                assert (await client.inspect(body["message_id"]))["remote_acked"] is True
                wrong = {**body, "site_id": "other"}
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    await client.enqueue(wrong)
                assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await server.stop(0)


async def test_auth_pause_until_rotation_and_queue_survives(tmp_path):
    class Transport:
        token = "old"
        calls = 0

        async def send(self, body):
            self.calls += 1
            return (
                Result("auth", "AUTHENTICATION_FAILED")
                if self.token == "old"
                else Result("accepted", received_at="2026-09-20T01:00:00Z")
            )

    with Outbox(tmp_path) as store:
        store.enqueue(message())
        transport = Transport()
        worker = DeliveryWorker(store, transport)
        await worker.step(now=0)
        await worker.step(now=10)
        assert transport.calls == 1
        assert store.stats()["pending_messages"] == 1
        transport.token = "new"
        await worker.step(now=20)
        assert transport.calls == 2
        assert store.stats()["pending_messages"] == 0


async def test_invalid_ack_retries_same_body_and_server_retry_after(tmp_path):
    class Transport:
        token = "token"
        calls = []

        async def send(self, body):
            self.calls.append(body)
            return Result("retry", "INVALID_ACK", retry_after=120)

    with Outbox(tmp_path) as store:
        store.enqueue(message())
        transport = Transport()
        worker = DeliveryWorker(store, transport)
        await worker.step(now=0)
        await worker.step(now=100)
        assert len(transport.calls) == 1
        await worker.step(now=121)
        assert transport.calls[0] == transport.calls[1]


async def test_observation_stream_overflow_visible_without_blocking_processing():
    broker = ObservationBroker(capacity=2)
    queue = broker.subscribe()
    for i in range(4):
        broker.publish({"sequence": i})
    assert broker.dropped == 2
    assert (await queue.get())["observation"]["sequence"] == 2
    assert (await queue.get())["dropped_observations"] == 2
    broker.unsubscribe(queue)


@pytest.mark.parametrize("outcome", ["accepted", "quarantine"])
async def test_inflight_message_survives_concurrent_capacity_eviction(tmp_path, outcome):
    with Outbox(tmp_path, limit_bytes=65536, reserve_bytes=16384) as store:
        body = message()
        store.enqueue(body)

        class Transport:
            token = "test"

            async def send(self, payload):
                if payload["message_id"] != body["message_id"]:
                    return Result("retry", "NETWORK_OR_TIMEOUT")
                for _ in range(20):
                    # Concurrent producers use the same single writer as delivery.
                    try:
                        await store.call("enqueue", message())
                    except CapacityError:
                        # Background input can be rejected at the physical limit;
                        # the already in-flight target must remain protected.
                        pass
                return Result(outcome, reason="SCHEMA_REJECTED", received_at="2026-09-20T01:00:00Z")

        await DeliveryWorker(store, Transport()).step(now=0)
        if outcome == "accepted":
            assert store.get(body["message_id"]) is None
        else:
            assert store.get(body["message_id"])["state"] == "quarantine"


async def test_retry_delay_starts_after_response(tmp_path):
    clock = [0]

    class Transport:
        token = "test"

        async def send(self, body):
            clock[0] = 4
            return Result("retry", retry_after=3)

    with Outbox(tmp_path) as store:
        store.enqueue(message())
        worker = DeliveryWorker(store, Transport(), clock=lambda: clock[0])
        await worker.step()
        assert worker.next_attempt == 7


async def test_status_flapping_cannot_starve_durable_queue(tmp_path):
    class Transport:
        token = "test"

        async def send(self, body):
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        store.enqueue(message())
        latest = {}
        worker = DeliveryWorker(store, Transport(), latest_status=latest)
        for i in range(4):
            latest["payload"] = {"message_id": f"status-{i}"}
            await worker.step(now=i * 2)
        assert store.stats()["pending_messages"] == 0


def test_backend_service_snapshot_preserves_loss_and_checks_current_revision(tmp_path):
    from ajin_edge.v2.runtime import LocalStatus, service_snapshots
    from test_v2_processing import configuration

    cfg = {
        "configuration": configuration(),
        "status_dir": str(tmp_path),
        "drivers": {"lidar-a": "a", "lidar-b": "b"},
    }
    status = LocalStatus(cfg, "lidar-processing")
    status.advanced()
    status.write(dropped_observations=3)
    result = next(s for s in service_snapshots(cfg) if s["service"] == "lidar-processing")
    assert result["state"] == "DEGRADED"
    assert "OBSERVATION_LOSS" in result["reason_codes"]


async def test_successful_local_delivery_clears_transient_error(tmp_path):
    import asyncio

    from ajin_edge.v2.runtime import LocalStatus, PendingSender
    from test_v2_processing import configuration

    class Client:
        async def enqueue(self, payload):
            return {"state": "pending"}

    status = LocalStatus(
        {"configuration": configuration(), "status_dir": str(tmp_path)}, "lidar-processing"
    )
    status.reason = "LOCAL_DELIVERY_UNAVAILABLE"
    sender = PendingSender(Client(), status)
    sender.offer(message())
    task = asyncio.create_task(sender.run())
    try:
        for _ in range(20):
            await asyncio.sleep(0.01)
            if not sender.queue:
                break
        assert status.reason == ""
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_loss_alert_recovers_without_resetting_counters():
    from ajin_edge.v2.runtime import LossTracker

    tracker = LossTracker()
    assert tracker.observe("driver", "instance-1", {"frame_loss": 21}, now=0)
    assert tracker.observe("driver", "instance-1", {"frame_loss": 21}, now=9)
    assert not tracker.observe("driver", "instance-1", {"frame_loss": 21}, now=10)
    assert tracker.observe("driver", "instance-1", {"frame_loss": 22}, now=12)
    assert not tracker.observe("driver", "instance-1", {"frame_loss": 22}, now=22)
    assert not tracker.observe("driver", "instance-2", {"frame_loss": 0}, now=23)
    assert tracker.observe("driver", "instance-2", {"frame_loss": 1}, now=24)
    assert tracker.observe("unversioned", None, {"frame_loss": 100}, now=0)
    assert not tracker.observe("unversioned", None, {"frame_loss": 100}, now=11)
    assert tracker.observe("unversioned", None, {"frame_loss": 1}, now=12)


def test_local_backlog_loss_reason_recovers_while_count_is_preserved(tmp_path, monkeypatch):
    import json

    import ajin_edge.v2.runtime as module
    from ajin_edge.v2.runtime import LocalStatus, PendingSender
    from test_v2_processing import configuration

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    status = LocalStatus(
        {"configuration": configuration(), "status_dir": str(tmp_path)}, "lidar-processing"
    )
    sender = PendingSender(None, status, capacity=1)
    sender.offer({"id": 1})
    sender.offer({"id": 2})
    assert status.reason == "LOCAL_DELIVERY_BACKLOG_LOSS"
    clock[0] = 10
    status.write()
    body = json.loads((tmp_path / "lidar-processing.json").read_bytes())
    assert body["local_dropped_messages"] == 1
    assert body["reason_codes"] == []


def test_service_snapshots_do_not_latch_historical_loss_or_clear_other_faults(tmp_path):
    from ajin_edge.v2.runtime import LocalStatus, LossTracker, service_snapshots
    from test_v2_processing import configuration

    config = {
        "configuration": configuration(),
        "status_dir": str(tmp_path),
        "drivers": {"lidar-a": "a", "lidar-b": "b"},
    }
    status = LocalStatus(config, "lidar-processing")
    tracker = LossTracker()
    status.write(dropped_observations=2686)

    def snapshot(now):
        return next(
            s
            for s in service_snapshots(config, loss_tracker=tracker, now=now)
            if s["service"] == "lidar-processing"
        )

    assert snapshot(0)["state"] == "DEGRADED"
    assert snapshot(10)["state"] == "HEALTHY"
    assert snapshot(10)["reason_codes"] == []
    status.reason = "DRIVER_UNAVAILABLE"
    status.write("DEGRADED", dropped_observations=2686)
    assert snapshot(11)["state"] == "DEGRADED"
    assert snapshot(11)["reason_codes"] == ["DRIVER_UNAVAILABLE"]
