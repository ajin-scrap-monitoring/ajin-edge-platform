"""Independent delivery scheduling; collection never awaits backend HTTP."""

import asyncio
import time
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
        sent = 0
        stopped = False
        reasons, progress, retries = [], [], []

        def dispatch(payload, is_status):
            nonlocal sent
            tasks[asyncio.create_task(self.transport.send(payload))] = (payload, is_status)
            sent += 1

        async def refill():
            # Keep four network slots. A finite turn also services token rotation,
            # status fairness and shutdown without retaining an unbounded batch.
            while len(tasks) < 4 and sent < 16 and not stopped and self.clock() - started < 1:
                selected = asyncio.create_task(self.store.call("next_ready", pin=True))
                try:
                    row = await asyncio.shield(selected)
                except asyncio.CancelledError:
                    # A running executor operation cannot be cancelled. Account
                    # for its selected pin before the outer cleanup releases it.
                    row = await selected
                    if row is not None:
                        rows[row["payload"]["message_id"]] = row
                    raise
                if row is None:
                    break
                rows[row["payload"]["message_id"]] = row
                dispatch(row["payload"], False)

        try:
            if use_status:
                self.last_status_attempt = now
                dispatch(status, True)
            await refill()
            if not tasks and status is not None:
                use_status = True
                self.last_status_attempt = now
                dispatch(status, True)
            if not tasks:
                return False
            self.previous_was_status = use_status
            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                acknowledgements, completed, errors = [], [], []
                for task in done:
                    payload, is_status = tasks.pop(task)
                    if not is_status:
                        completed.append(payload["message_id"])
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
                            await self.store.call(
                                "quarantine", payload["message_id"], result.reason
                            )
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
                if acknowledgements:
                    await self.store.call("delivered_many", acknowledgements)
                for mid in completed:
                    await self.store.call("unpin", mid)
                    del rows[mid]
                if errors:
                    raise errors[0]
                await refill()
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
            for mid in rows:
                await self.store.call("unpin", mid)
        return True
