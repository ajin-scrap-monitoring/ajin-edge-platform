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
        rows = []
        try:
            # A burst of EVIDENCE must not hide the two 1Hz feeds. Reserve at
            # most two slots while leaving at least one slot for durable work
            # even when a status snapshot is also due.
            for priority in ("periodic", "assessment"):
                row = await self.store.call("next_ready", pin=True, priority=priority)
                if row is not None:
                    rows.append(row)
            for _ in range(4 - int(use_status) - len(rows)):
                row = await self.store.call("next_ready", pin=True)
                if row is None:
                    break
                rows.append(row)
            if not rows and status is not None:
                use_status = True
            payloads = ([status] if use_status else []) + [r["payload"] for r in rows]
            if not payloads:
                return False
            self.previous_was_status = use_status
            if use_status:
                self.last_status_attempt = now
            # Bound network work and memory; each request retains its own body,
            # idempotency key and storage ACK. Arrival order was never guaranteed.
            results = await asyncio.gather(
                *(self.transport.send(p) for p in payloads), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            acknowledgements = []
            progress = []
            retries = []
            reasons = []
            for index, (payload, result) in enumerate(zip(payloads, results, strict=True)):
                is_status = use_status and index == 0
                if result.reason:
                    reasons.append(result.reason)
                if result.kind == "accepted":
                    progress.append(result.received_at)
                    if not is_status:
                        acknowledgements.append((payload["message_id"], result.received_at))
                elif result.kind == "auth":
                    self.blocked_token = self.transport.token
                elif result.kind == "configuration":
                    self.configuration_blocked = True
                elif result.kind == "quarantine":
                    if not is_status:
                        await self.store.call("quarantine", payload["message_id"], result.reason)
                else:
                    retries.append(result.retry_after)
                if (
                    is_status
                    and result.kind in ("accepted", "quarantine")
                    and self.latest_status.get("payload", {}).get("message_id")
                    == payload["message_id"]
                ):
                    self.latest_status.clear()
            if acknowledgements:
                await self.store.call("delivered_many", acknowledgements)
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
            for row in rows:
                await self.store.call("unpin", row["payload"]["message_id"])
        return True
