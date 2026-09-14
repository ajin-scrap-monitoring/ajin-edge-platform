"""TLS-only backend contract. Transport acknowledgement is explicitly correlated."""

import asyncio
import math
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .contracts import canonical_json


def validate_https_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("endpoint must be HTTPS without credentials, query or fragment")
    return url


def read_token(path):
    if Path(path).stat().st_size > 4096:
        raise ValueError("credential file too large")
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("invalid bearer credential")
    return token


def retry_after(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, min(seconds, 60.0)) if math.isfinite(seconds) else 60.0


@dataclass(frozen=True)
class DeliveryResult:
    kind: str
    reason: str
    retry_after: float = 0.0


class RetryBackoff:
    def __init__(self, *, jitter=random.random):
        self.delay = 1.0
        self.stable_since = None
        self.jitter = jitter

    def failure(self):
        self.stable_since = None
        delay = min(60.0, self.delay + self.jitter() * self.delay * 0.2)
        self.delay = min(60.0, self.delay * 2)
        return delay

    def success(self, *, now=None):
        now = time.monotonic() if now is None else now
        if self.stable_since is None:
            self.stable_since = now
        if now - self.stable_since >= 30:
            self.delay = 1.0


class DeliveryClient:
    def __init__(self, url, token, *, client, total_timeout=5):
        self.url = validate_https_url(url)
        self.token, self.client = token, client
        if not math.isfinite(total_timeout) or total_timeout <= 0:
            raise ValueError("total request timeout must be positive")
        self.total_timeout = total_timeout

    async def send(self, payload, *, heartbeat=False):
        try:
            async with asyncio.timeout(self.total_timeout):
                return await self._send(payload, heartbeat=heartbeat)
        except TimeoutError:
            return DeliveryResult("retry", "REQUEST_TIMEOUT")

    async def _send(self, payload, *, heartbeat=False):
        mid = payload.get("measurement_id")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if mid:
            headers["Idempotency-Key"] = mid
        try:
            # Stream the response to enforce a memory bound even on a broken backend.
            async with self.client.stream(
                "POST",
                self.url,
                content=canonical_json(payload),
                headers=headers,
                timeout=5,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status in (401, 403):
                    return DeliveryResult("fatal", "AUTHENTICATION_FAILED")
                if status in (408, 429) or status >= 500:
                    return DeliveryResult(
                        "retry", f"HTTP_{status}", retry_after(response.headers.get("Retry-After"))
                    )
                if heartbeat and 200 <= status < 300:
                    return DeliveryResult("accepted", "ACCEPTED")
                if not (200 <= status < 300 or status == 409):
                    return DeliveryResult("quarantine", f"HTTP_{status}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        return DeliveryResult("retry", "ACK_TOO_LARGE")
                import json

                try:
                    ack = json.loads(body)
                except (ValueError, UnicodeError):
                    return DeliveryResult("retry", "INVALID_ACK")
                if isinstance(ack, dict) and ack.get("measurement_id") == mid:
                    if (
                        status == 409
                        and ack.get("duplicate") is True
                        or 200 <= status < 300
                        and ack.get("accepted") is True
                    ):
                        return DeliveryResult("accepted", "ACCEPTED")
                return DeliveryResult("quarantine" if status == 409 else "retry", "ACK_MISMATCH")
        except httpx.HTTPError:
            return DeliveryResult("retry", "NETWORK_ERROR")
