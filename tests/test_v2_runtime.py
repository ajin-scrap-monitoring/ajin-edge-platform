import grpc
import pytest
from ajin_edge.v2.ipc import DeliveryStub, ObservationBroker, serve_delivery
from ajin_edge.v2.outbox import Outbox
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
                for _ in range(20):
                    store.enqueue(message())
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
