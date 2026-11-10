import asyncio
import threading

import pytest
from ajin_edge.v2.outbox import Outbox
from ajin_edge.v2.transport import Result
from ajin_edge.v2.worker import DeliveryWorker
from test_v2_contracts import message


async def test_free_slot_refills_before_slow_peer_finishes_with_four_requests_max(tmp_path):
    bodies = [message() for _ in range(8)]
    release = asyncio.Event()
    refilled = asyncio.Event()
    sent = []
    active = peak = 0

    class Transport:
        token = "test"

        async def send(self, body):
            nonlocal active, peak
            sent.append(body["message_id"])
            active += 1
            peak = max(peak, active)
            if len(sent) > 4:
                refilled.set()
            try:
                if body["message_id"] == bodies[-1]["message_id"]:
                    await release.wait()
                else:
                    await asyncio.sleep(0)
                return Result("accepted", received_at="2026-09-20T01:00:00Z")
            finally:
                active -= 1

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step(now=0))
        try:
            await asyncio.wait_for(refilled.wait(), 1)
            assert not release.is_set()
            assert peak <= 4
            assert len(store.inflight) <= 4
        finally:
            release.set()
            await task
        assert len(sent) == len(set(sent)) == 8
        assert store.stats()["pending_messages"] == 0
        assert not store.inflight


@pytest.mark.parametrize("outcome", ["auth", "configuration", "retry"])
async def test_failure_stops_refill_and_keeps_inflight_receipts(tmp_path, outcome):
    bodies = [message() for _ in range(8)]
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            if body["message_id"] == bodies[-1]["message_id"]:
                return Result(outcome, "SYNTHETIC_FAILURE")
            await asyncio.sleep(0.02)
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        worker = DeliveryWorker(store, Transport())
        await worker.step(now=0)
        assert len(sent) == 4
        if outcome == "auth":
            assert worker.blocked_token == "test"
        elif outcome == "configuration":
            assert worker.configuration_blocked
        else:
            assert worker.next_attempt > 0
        assert store.stats()["pending_messages"] == 5
        assert not store.inflight


async def test_cancel_releases_pins_and_preserves_unconfirmed_body(tmp_path):
    entered = asyncio.Event()
    body = message()

    class Transport:
        token = "test"

        async def send(self, _body):
            entered.set()
            await asyncio.Event().wait()

    with Outbox(tmp_path) as store:
        store.enqueue(body)
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store.inflight
        assert store.get(body["message_id"])["payload"] == body
        assert not store.inspect(body["message_id"])["remote_acked"]


async def test_cancellation_during_executor_selection_releases_late_pin(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    with Outbox(tmp_path) as store:
        body = message()
        store.enqueue(body)
        original = store.next_ready

        def delayed(**kwargs):
            entered.set()
            release.wait(2)
            return original(**kwargs)

        monkeypatch.setattr(store, "next_ready", delayed)
        worker = DeliveryWorker(store, object())
        task = asyncio.create_task(worker.step())
        await asyncio.to_thread(entered.wait, 1)
        assert entered.is_set()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await store.call("stats")  # Flush any already-running executor selection.
        assert not store.inflight
        assert store.stats()["pending_messages"] == 1


async def test_fast_successful_turn_is_finite_and_all_unconfirmed_data_remains(tmp_path):
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for _ in range(24):
            store.enqueue(message())
        await DeliveryWorker(store, Transport(), clock=lambda: 0).step(now=0)
        assert len(sent) == len(set(sent)) == 16
        assert store.stats()["pending_messages"] == 8
        assert not store.inflight
