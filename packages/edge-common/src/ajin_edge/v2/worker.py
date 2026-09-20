"""Independent delivery scheduling; collection never awaits backend HTTP."""

import time

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
        row = None if use_status else await self.store.call("next_ready", pin=True)
        if row is None and status is not None:
            use_status = True
        payload = status if use_status else row["payload"] if row else None
        if payload is None:
            return False
        self.previous_was_status = use_status
        if use_status:
            self.last_status_attempt = now
        try:
            result = await self.transport.send(payload)
            self.reason = result.reason
            if result.kind == "accepted":
                if not use_status:
                    await self.store.call("delivered", payload["message_id"], result.received_at)
                elif (
                    self.latest_status.get("payload", {}).get("message_id") == payload["message_id"]
                ):
                    self.latest_status.clear()
                self.backoff.success()
                self.last_progress_at = result.received_at
            elif result.kind == "auth":
                self.blocked_token = self.transport.token
            elif result.kind == "configuration":
                self.configuration_blocked = True
            elif result.kind == "quarantine":
                if not use_status:
                    await self.store.call("quarantine", payload["message_id"], result.reason)
                elif (
                    self.latest_status.get("payload", {}).get("message_id") == payload["message_id"]
                ):
                    self.latest_status.clear()
            else:
                finished = now + max(0, self.clock() - started)
                self.next_attempt = finished + max(self.backoff.failure(), result.retry_after)
        finally:
            if row is not None:
                await self.store.call("unpin", payload["message_id"])
        return True
