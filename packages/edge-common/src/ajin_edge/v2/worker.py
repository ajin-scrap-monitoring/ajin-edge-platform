"""Independent delivery scheduling; collection never awaits backend HTTP."""

import asyncio
import time
from collections import deque
from datetime import datetime

from .transport import Backoff


class DeliveryWorker:
    def __init__(self, store, transport, *, latest_status=None, clock=time.monotonic):
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
        ack_task = None
        ack_batch = []
        quarantines = []
        sent = 0
        stopped = False
        reasons, progress, retries = [], [], []

        def dispatch(payload, is_status):
            nonlocal sent
            tasks[asyncio.create_task(self.transport.send(payload))] = (payload, is_status)
            sent += 1

        def refill():
            # Keep six network slots. A finite turn also services token rotation,
            # status fairness and shutdown without retaining an unbounded batch.
            while (
                ready
                and len(tasks) < 6
                and sent < 16
                and not stopped
                and self.clock() - send_started < 1
            ):
                row = ready.popleft()
                dispatch(row["payload"], False)

        def start_flush(*, force=False):
            nonlocal ack_task, ack_batch
            if ack_task is None and acknowledgements and (force or len(acknowledgements) >= 8):
                ack_batch = acknowledgements[:8]
                del acknowledgements[: len(ack_batch)]
                ack_task = asyncio.create_task(
                    self.store.call("delivered_many", ack_batch, release_pins=True)
                )

        async def finish_flush():
            nonlocal ack_task, ack_batch
            await asyncio.shield(ack_task)
            for mid, _ in ack_batch:
                rows.pop(mid, None)
            ack_task, ack_batch = None, []

        async def drain_acks():
            if ack_task is not None:
                await finish_flush()
            while acknowledgements:
                start_flush(force=True)
                await finish_flush()

        try:
            # Select a bounded turn once. Network slot refills then never wait
            # behind enqueue/fsync jobs on the storage executor. Pins retain
            # both unsent bodies and remote ACKs until durable confirmation.
            selected = asyncio.create_task(self.store.call("next_ready_many", 16))
            try:
                batch = await asyncio.shield(selected)
            except asyncio.CancelledError:
                batch = await selected
                rows.update((r["payload"]["message_id"], r) for r in batch)
                raise
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
            while tasks or ack_task is not None:
                waiting = set(tasks)
                if ack_task is not None:
                    waiting.add(ack_task)
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if ack_task is not None and ack_task in done:
                    done.remove(ack_task)
                    await finish_flush()
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
                if errors:
                    await drain_acks()
                    raise errors[0]
            await drain_acks()
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
            await self.store.call("unpin_many", tuple(rows))
        return True
