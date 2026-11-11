import asyncio

import pytest
from ajin_edge.v2.outbox import CapacityError, Outbox
from ajin_edge.v2.transport import Result
from ajin_edge.v2.worker import DeliveryWorker
from test_v2_contracts import message as periodic_message


def message():
    body = periodic_message()
    body.update(
        delivery_kind="EVIDENCE",
        selection_policy="REFERENCED_SCANS",
        report_interval_ms=None,
        pair_state="NOT_APPLICABLE",
        pair_delta_ms=None,
        assessment_ids=["test-assessment"],
    )
    body["sensors"] = [s for s in body["sensors"] if s["scan"] is not None]
    return body


async def test_independent_pending_posts_can_progress_concurrently_without_duplicate_selection(
    tmp_path,
):
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            if len(sent) == 3:
                entered.set()
            await release.wait()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for _ in range(3):
            store.enqueue(message())
        worker = DeliveryWorker(store, Transport())
        task = asyncio.create_task(worker.step(now=0))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            assert len(set(sent)) == 3
            assert len(store.inflight) == 3
        finally:
            release.set()
            await task
        assert store.stats()["pending_messages"] == 0
        assert not store.inflight


async def test_mixed_http_results_keep_failed_body_and_persist_other_ack(tmp_path):
    bodies = [message() for _ in range(3)]
    outcomes = dict(
        zip(
            [b["message_id"] for b in bodies],
            [
                Result("retry", "NETWORK_OR_TIMEOUT"),
                Result("accepted", received_at="2026-09-20T01:00:00Z"),
                Result("quarantine", "SCHEMA_REJECTED"),
            ],
            strict=True,
        )
    )

    class Transport:
        token = "test"

        async def send(self, body):
            return outcomes[body["message_id"]]

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        worker = DeliveryWorker(store, Transport())
        await worker.step(now=0)
        assert store.stats()["pending_messages"] == 1
        assert store.stats()["quarantined_messages"] == 1
        assert store.inspect(bodies[1]["message_id"])["remote_acked"]
        assert store.get(bodies[0]["message_id"])["payload"] == bodies[0]
        assert worker.next_attempt > 0


def test_batched_ack_is_atomic_on_write_failure_and_recovers_after_restart(tmp_path, monkeypatch):
    with Outbox(tmp_path) as store:
        assert hasattr(store, "delivered_many"), "batched durable acknowledgements are missing"
        bodies = [message() for _ in range(3)]
        for body in bodies:
            store.enqueue(body)
        original = store._save_meta

        def fail():
            raise CapacityError("metadata unavailable")

        monkeypatch.setattr(store, "_save_meta", fail)
        with pytest.raises(CapacityError):
            store.delivered_many([(b["message_id"], "2026-09-20T01:00:00Z") for b in bodies])
        assert all(not store.inspect(b["message_id"])["remote_acked"] for b in bodies)
        monkeypatch.setattr(store, "_save_meta", original)
        store.delivered_many([(b["message_id"], "2026-09-20T01:00:00Z") for b in bodies])
    with Outbox(tmp_path) as store:
        assert store.stats()["pending_messages"] == 0
        assert all(store.inspect(b["message_id"])["remote_acked"] for b in bodies)
