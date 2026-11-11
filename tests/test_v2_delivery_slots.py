import asyncio
import threading

import pytest
from ajin_edge.v2.outbox import Outbox
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


async def test_free_slot_refills_before_slow_peer_finishes_with_six_requests_max(tmp_path):
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
            if len(sent) > 6:
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
            assert peak <= 6
            assert len(store.inflight) <= 16  # Includes bounded, not-yet-durable ACKs.
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
        assert len(sent) == 6
        if outcome == "auth":
            assert worker.blocked_token == "test"
        elif outcome == "configuration":
            assert worker.configuration_blocked
        else:
            assert worker.next_attempt > 0
        assert store.stats()["pending_messages"] == 3
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
        original = store.get

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "get", delayed)
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
        for _ in range(40):
            store.enqueue(message())
        await DeliveryWorker(store, Transport(), clock=lambda: 0).step(now=0)
        assert len(sent) == len(set(sent)) == 32
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
        task = asyncio.create_task(DeliveryWorker(store, Transport()).step())
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
        original = store.next_ordered_many

        def delayed(limit):
            clock[0] += 2
            return original(limit)

        monkeypatch.setattr(store, "next_ordered_many", delayed)
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
            store.next_ordered_many()
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


async def test_six_network_slots_are_available_and_bounded(tmp_path):
    release, full = asyncio.Event(), asyncio.Event()
    active = peak = 0

    class Transport:
        token = "test"

        async def send(self, body):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 6:
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
            assert active == peak == 6
        finally:
            release.set()
            await task
        assert peak == 6
        assert not store.inflight


async def test_next_batch_flows_before_slowest_previous_request_finishes(tmp_path, monkeypatch):
    release, next_batch = asyncio.Event(), asyncio.Event()
    bodies = [message() for _ in range(24)]
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            if len(sent) == 24:
                next_batch.set()
            if body["message_id"] == bodies[-1]["message_id"]:
                await release.wait()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        original = store.next_ordered_many

        def bounded(limit):
            rows = original(limit)
            assert len(store.inflight) <= 16
            return rows

        monkeypatch.setattr(store, "next_ready_many", bounded)
        task = asyncio.create_task(DeliveryWorker(store, Transport(), clock=lambda: 0).step())
        try:
            await asyncio.wait_for(next_batch.wait(), 1)
            assert not release.is_set()
        finally:
            release.set()
            await task
        assert len(set(sent)) == 24
        assert not store.inflight
        assert store.stats()["pending_messages"] == 0


async def test_cancel_with_replacement_selection_and_ack_queued_preserves_bodies(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    http_release, write_queued = asyncio.Event(), asyncio.Event()
    bodies = [message() for _ in range(32)]
    sent = []

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            if len(sent) > 8:
                await http_release.wait()
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for body in bodies:
            store.enqueue(body)
        original_select, original_call = store.next_ordered_many, store.call
        selections = 0

        def select(limit):
            nonlocal selections
            selections += 1
            if selections == 2:
                entered.set()
                release.wait(3)
            return original_select(limit)

        async def call(method, *args, **kwargs):
            if method == "delivered_many" and entered.is_set():
                write_queued.set()
            return await original_call(method, *args, **kwargs)

        monkeypatch.setattr(store, "next_ordered_many", select)
        monkeypatch.setattr(store, "call", call)
        task = asyncio.create_task(DeliveryWorker(store, Transport(), clock=lambda: 0).step())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            http_release.set()
            await asyncio.wait_for(write_queued.wait(), 1)
            task.cancel()
        finally:
            http_release.set()
            release.set()
            if not task.done():
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not store.inflight
    with Outbox(tmp_path) as store:
        for body in bodies:
            mid = body["message_id"]
            assert store.inspect(mid)["remote_acked"] or store.get(mid)["payload"] == body


async def test_replacement_selected_after_deadline_is_released_without_sending(
    tmp_path, monkeypatch
):
    clock = [0.0]
    sent, replacement_ids = [], set()

    class Transport:
        token = "test"

        async def send(self, body):
            sent.append(body["message_id"])
            await asyncio.sleep(0)
            return Result("accepted", received_at="2026-09-20T01:00:00Z")

    with Outbox(tmp_path) as store:
        for _ in range(32):
            store.enqueue(message())
        original = store.next_ordered_many
        selections = 0

        def select(limit):
            nonlocal selections
            selections += 1
            rows = original(limit)
            if selections == 2:
                clock[0] = 3
                replacement_ids.update(r["payload"]["message_id"] for r in rows)
            return rows

        monkeypatch.setattr(store, "next_ordered_many", select)
        await DeliveryWorker(store, Transport(), clock=lambda: clock[0]).step()
        assert replacement_ids
        assert not replacement_ids.intersection(sent)
        assert not store.inflight
        assert all(store.get(mid)["state"] == "pending" for mid in replacement_ids)
