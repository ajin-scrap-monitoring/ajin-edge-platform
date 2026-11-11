"""Independent delivery scheduling; collection never awaits backend HTTP."""

import asyncio
import time
from collections import deque
from datetime import datetime

from .transport import Backoff


class DeliveryWorker:
    def __init__(
        self, store, transport, *, latest_status=None, clock=time.monotonic, network_slots=6
    ):
        if type(network_slots) is not int or not 1 <= network_slots <= 6:
            raise ValueError("network slots must be between 1 and 6")
        self.network_slots = network_slots
        self.store, self.transport = store, transport
        self.latest_status = latest_status if latest_status is not None else {}
        self.backoff = Backoff()
        self.next_attempt = 0
        self.blocked_token = None
        self.configuration_blocked = False
        self.reason = ""
        self.last_progress_at = None
        self.last_status_attempt = -10
        self.clock = clock
        self.previous_was_status = False

    async def step(self, *, now=None):
        started = self.clock()
        send_started = started
        now = started if now is None else now
        if self.blocked_token and self.blocked_token != self.transport.token:
            self.blocked_token = None
            self.next_attempt = 0
            self.backoff.success()
        if self.blocked_token or self.configuration_blocked or now < self.next_attempt:
            return False
        status = self.latest_status.get("payload")
        use_status = (
            status is not None
            and now - self.last_status_attempt >= 1
            and not self.previous_was_status
        )
        rows = {}
        tasks = {}
        ready = deque()
        acknowledgements = []
        selection_task = None
        selection_exhausted = False
        ack_task = None
        ack_batch = []
        quarantines = []
        sent = 0
        stopped = False
        periodic_turn = False
        reasons, progress, retries = [], [], []

        def dispatch(payload, is_status):
            nonlocal sent
            row = rows.get(payload["message_id"])
            if row is not None and hasattr(self.transport, "record_registration"):
                self.transport.record_registration(payload["message_id"], row["order"])
            tasks[asyncio.create_task(self.transport.send(payload))] = (payload, is_status)
            sent += 1

        def refill():
            # Keep a bounded number of network slots. A finite turn also services token rotation,
            # status fairness and shutdown without retaining an unbounded batch.
            while (
                ready
                and len(tasks) < self.network_slots
                and sent < 32
                and not stopped
                and self.clock() - send_started < 2
            ):
                periodic = next(
                    (r for r in ready if r["payload"].get("delivery_kind") == "PERIODIC"), None
                )
                if periodic is not None:
                    ready.remove(periodic)
                    row = periodic
                else:
                    # Keep the periodic lane available while its durable ACK is
                    # pending, rather than filling it with slow evidence HTTP.
                    if (
                        periodic_turn
                        and self.network_slots > 1
                        and sum(p.get("delivery_kind") != "PERIODIC" for p, _ in tasks.values())
                        >= self.network_slots - 1
                    ):
                        break
                    row = ready.popleft()
                dispatch(row["payload"], False)

        def start_selection():
            nonlocal selection_task
            available = min(16 - len(rows), 32 - sent - len(ready))
            if (
                selection_task is None
                and not selection_exhausted
                and not stopped
                and (
                    len(ready) < self.network_slots
                    or (
                        periodic_turn
                        and not any(
                            r["payload"].get("delivery_kind") == "PERIODIC" for r in rows.values()
                        )
                    )
                )
                and available > 0
                and self.clock() - send_started < 2
            ):
                # Only one selection can reserve these slots. ACK completion
                # may free additional slots, but cannot increase this batch.
                selection_task = asyncio.create_task(
                    self.store.call("next_ordered_many", available)
                )

        def start_flush(*, force=False):
            nonlocal ack_task, ack_batch
            periodic_waiting = any(
                rows[mid]["payload"].get("delivery_kind") == "PERIODIC"
                for mid, _ in acknowledgements
            )
            if (
                ack_task is None
                and acknowledgements
                and (force or periodic_waiting or len(acknowledgements) >= 8)
            ):
                ack_batch = acknowledgements[:8]
                del acknowledgements[: len(ack_batch)]
                ack_task = asyncio.create_task(
                    self.store.call("delivered_many", ack_batch, release_pins=True)
                )

        async def finish_flush():
            nonlocal ack_task, ack_batch, selection_exhausted
            await asyncio.shield(ack_task)
            for mid, _ in ack_batch:
                rows.pop(mid, None)
            ack_task, ack_batch = None, []
            selection_exhausted = False

        async def drain_acks():
            if ack_task is not None:
                await finish_flush()
            while acknowledgements:
                start_flush(force=True)
                await finish_flush()

        try:
            # Begin with a bounded selection, then overlap replacement reads
            # with HTTP after durable ACKs free slots. Pins retain unsent bodies
            # and remote ACKs until durable confirmation.
            selection_task = asyncio.create_task(self.store.call("next_ordered_many", 16))
            batch = await asyncio.shield(selection_task)
            selection_task = None
            selection_exhausted = not batch
            periodic_turn = any(r["payload"].get("delivery_kind") == "PERIODIC" for r in batch)
            ready.extend(batch)
            rows.update((r["payload"]["message_id"], r) for r in batch)
            send_started = self.clock()
            if use_status:
                self.last_status_attempt = now
                dispatch(status, True)
            refill()
            if not tasks and status is not None:
                use_status = True
                self.last_status_attempt = now
                dispatch(status, True)
            if not tasks:
                return False
            self.previous_was_status = use_status
            while tasks or ack_task is not None or selection_task is not None:
                waiting = set(tasks)
                if ack_task is not None:
                    waiting.add(ack_task)
                if selection_task is not None:
                    waiting.add(selection_task)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if ack_task is not None and ack_task in done:
                    done.remove(ack_task)
                    await finish_flush()
                if selection_task is not None and selection_task in done:
                    done.remove(selection_task)
                    batch = selection_task.result()
                    selection_task = None
                    selection_exhausted = not batch
                    periodic_turn |= any(
                        r["payload"].get("delivery_kind") == "PERIODIC" for r in batch
                    )
                    ready.extend(batch)
                    rows.update((r["payload"]["message_id"], r) for r in batch)
                errors = []
                for task in done:
                    payload, is_status = tasks.pop(task)
                    try:
                        result = task.result()
                    except BaseException as error:
                        errors.append(error)
                        stopped = True
                        continue
                    if result.reason:
                        reasons.append(result.reason)
                    if result.kind == "accepted":
                        progress.append(result.received_at)
                        if not is_status:
                            acknowledgements.append((payload["message_id"], result.received_at))
                    elif result.kind == "auth":
                        self.blocked_token = self.transport.token
                        stopped = True
                    elif result.kind == "configuration":
                        self.configuration_blocked = True
                        stopped = True
                    elif result.kind == "quarantine":
                        if not is_status:
                            quarantines.append((payload["message_id"], result.reason))
                    else:
                        retries.append(result.retry_after)
                        stopped = True
                    if (
                        is_status
                        and result.kind in ("accepted", "quarantine")
                        and self.latest_status.get("payload", {}).get("message_id")
                        == payload["message_id"]
                    ):
                        self.latest_status.clear()
                refill()
                start_flush(force=not tasks or bool(errors))
                start_selection()
                if errors:
                    await drain_acks()
                    raise errors[0]
            await drain_acks()
            if quarantines:
                # All HTTP and selection tasks have settled. Unsent/retry rows
                # no longer need pins; retaining them could prevent the atomic
                # quarantine replacement from reserving space at capacity.
                targets = {mid for mid, _ in quarantines}
                releasable = tuple(mid for mid in rows if mid not in targets)
                await self.store.call("unpin_many", releasable)
                for mid in releasable:
                    rows.pop(mid)
            for mid, reason in quarantines:
                await self.store.call("quarantine", mid, reason)
            self.reason = reasons[0] if reasons else ""
            if progress:
                self.last_progress_at = max(
                    [*progress, *([self.last_progress_at] if self.last_progress_at else [])],
                    key=datetime.fromisoformat,
                )
            if retries:
                finished = now + max(0, self.clock() - started)
                self.next_attempt = finished + max(self.backoff.failure(), *retries)
            else:
                self.backoff.success()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if ack_task is not None:
                # A queued/running durable write must settle before releasing
                # its pins; cancellation may replay HTTP but never drops a body.
                await asyncio.gather(ack_task, return_exceptions=True)
            if selection_task is not None:
                selected = await asyncio.gather(selection_task, return_exceptions=True)
                if not isinstance(selected[0], BaseException):
                    rows.update((r["payload"]["message_id"], r) for r in selected[0])
            await self.store.call("unpin_many", tuple(rows))
        return True
