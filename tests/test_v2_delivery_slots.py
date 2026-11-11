import asyncio
import threading

import pytest
from ajin_edge.v2.outbox import Outbox
from ajin_edge.v2.transport import Result
from ajin_edge.v2.worker import DeliveryWorker
from test_v2_contracts import message


async def test_free_slot_refills_before_slow_peer_finishes_with_eight_requests_max(tmp_path):
    bodies = [message() for _ in range(12)]
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
            if len(sent) > 8:
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
            assert peak <= 8
            assert len(store.inflight) <= 16  # Includes bounded, not-yet-durable ACKs.
        finally:
            release.set()
            await task
        assert len(sent) == len(set(sent)) == 12
        assert store.stats()["pending_messages"] == 0
        assert not store.inflight


@pytest.mark.parametrize("outcome", ["auth", "configuration", "retry"])
async def test_failure_stops_refill_and_keeps_inflight_receipts(tmp_path, outcome):
    bodies = [message() for _ in range(12)]
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
        assert len(sent) == 8
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


async def test_network_slots_continue_while_ack_batch_waits_for_storage(tmp_path, monkeypatch):
    ack_entered, release = threading.Event(), threading.Event()
    refilled = asyncio.Event()
    sent = []
    http_release = asyncio.Event()

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            ordinal = len(sent)
            if ordinal >= 15:
                refilled.set()
            if ordinal > 8:
                await http_release.wait()
            await asyncio.sleep(0)
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for _ in range(16):
            store.enqueue(message())
        original = store.delivered_many

        def delayed(receipts, **kwargs):
            ack_entered.set()
            release.wait(3)
            return original(receipts, **kwargs)

        monkeypatch.setattr(store, "delivered_many", delayed)
        task = asyncio.create_task(DeliveryWorker(store, Transport(), network_slots=6).step())
        try:
            assert await asyncio.to_thread(ack_entered.wait, 1)
            assert len(sent) == 14
            http_release.set()
            await asyncio.wait_for(refilled.wait(), 1)
            assert not release.is_set()
            assert store.get(sent[0]) is not None
            assert not store.inspect(sent[0])["remote_acked"]
        finally:
            http_release.set()
            release.set()
            await task
        assert store.stats()["pending_messages"] == 0
        assert not store.inflight


async def test_slow_selection_does_not_prevent_any_network_dispatch(tmp_path, monkeypatch):
    clock = [0.0]
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        store.enqueue(message())
        original = store.next_ready_many

        def delayed(limit):
            clock[0] += 2
            return original(limit)

        monkeypatch.setattr(store, "next_ready_many", delayed)
        await DeliveryWorker(store, Transport(), clock=lambda: clock[0]).step()
        assert len(sent) == 1
        assert not store.index


def test_partial_selection_error_releases_every_new_pin(tmp_path, monkeypatch):
    with Outbox(tmp_path) as store:
        for _ in range(4):
            store.enqueue(message())
        original = store.get
        reads = 0

        def failing(mid):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise OSError("read interrupted")
            return original(mid)

        monkeypatch.setattr(store, "get", failing)
        with pytest.raises(OSError):
            store.next_ready_many()
        assert not store.inflight
        assert store.stats()["pending_messages"] == 4


async def test_cancel_during_ack_flush_keeps_unconfirmed_data_replayable(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    bodies = [message() for _ in range(16)]

    class Transport:
        token = "test"

        async def send(self, body):
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        original = store.delivered_many

        def delayed(receipts, **kwargs):
            entered.set()
            release.wait(3)
            return original(receipts, **kwargs)

        monkeypatch.setattr(store, "delivered_many", delayed)
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step())
        await asyncio.to_thread(entered.wait, 1)
        assert entered.is_set()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store.inflight
        for body in bodies:
            mid = body["message_id"]
            assert store.inspect(mid)["remote_acked"] or store.get(mid)["payload"] == body
    with Outbox(tmp_path) as store:
        for body in bodies:
            mid = body["message_id"]
            assert store.inspect(mid)["remote_acked"] or store.get(mid)["payload"] == body


async def test_eight_network_slots_are_available_and_bounded(tmp_path):
    release, full = asyncio.Event(), asyncio.Event()
    active = peak = 0

    class Transport:
        token = "test"

        async def send(self, body):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 8:
                full.set()
            try:
                await release.wait()
                return Result("accepted", received_at="2026-09-20T01:00:00Z")
            finally:
                active -= 1

    with Outbox(tmp_path) as store:
        for _ in range(16):
            store.enqueue(message())
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step())
        try:
            await asyncio.wait_for(full.wait(), 0.5)
            assert active == peak == 8
        finally:
            release.set()
            await task
        assert peak == 8
        assert not store.inflight
