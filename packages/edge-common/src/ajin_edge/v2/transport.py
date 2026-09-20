"""v2 stored ACK protocol. No v1 409-as-success compatibility."""

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .processing import canonical

PATHS = {
    "lidar_measurement": "measurements",
    "edge_assessment": "assessments",
    "edge_status": "status",
    "edge_configuration": "configurations",
}


@dataclass(frozen=True)
class Result:
    kind: str
    reason: str = ""
    retry_after: float = 0
    received_at: str | None = None


class Backoff:
    def __init__(self, *, random=random.random):
        self.attempt = 0
        self.random = random

    def failure(self):
        base = min(30, 2 ** min(self.attempt, 5))
        self.attempt += 1
        return min(30, base * (1 + 0.2 * self.random()))

    def success(self):
        self.attempt = 0


class DeliveryClient:
    def __init__(self, base_url, token, client, *, timeout=5):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("a credential-free HTTPS origin is required")
        if not token or not token.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError("invalid bearer token")
        self.base_url, self.token, self.client = base_url.rstrip("/"), token, client
        self.timeout = timeout

    async def send(self, payload):
        try:
            return await asyncio.wait_for(self._send(payload), self.timeout)
        except (TimeoutError, httpx.HTTPError):
            return Result("retry", "NETWORK_OR_TIMEOUT")

    async def _send(self, payload):
        url = self.base_url + "/api/edge/v2/" + PATHS[payload["message_type"]]
        async with self.client.stream(
            "POST",
            url,
            content=canonical(payload),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Idempotency-Key": payload["message_id"],
            },
            timeout=self.timeout,
            follow_redirects=False,
        ) as response:
            code = response.status_code
            if code in (401, 403):
                return Result("auth", "AUTHENTICATION_FAILED")
            if code in (408, 429) or code >= 500:
                delay = self._retry_after(response.headers.get("Retry-After", ""))
                return Result("retry", f"HTTP_{code}", delay)
            if code in (400, 422, 409, 413):
                reason = {
                    400: "SCHEMA_REJECTED",
                    422: "SCHEMA_REJECTED",
                    409: "IDEMPOTENCY_CONFLICT",
                    413: "PAYLOAD_TOO_LARGE",
                }[code]
                return Result("quarantine", reason)
            if not 200 <= code < 300:
                return Result("configuration", f"HTTP_{code}")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > 65536:
                    return Result("retry", "INVALID_ACK")
                data.extend(chunk)
            try:
                ack = json.loads(data)
                if (
                    not isinstance(ack, dict)
                    or ack.get("message_id") != payload["message_id"]
                    or ack.get("accepted") is not True
                    or type(ack.get("duplicate")) is not bool
                    or code not in (200, 201)
                    or ack["duplicate"] != (code == 200)
                ):
                    raise ValueError("invalid ACK")
                received = ack["received_at"]
                if not isinstance(received, str) or not received.endswith(("Z", "+00:00")):
                    raise ValueError("UTC ACK required")
                datetime.fromisoformat(received.replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError, UnicodeError):
                return Result("retry", "INVALID_ACK")
            return Result("accepted", received_at=received)

    @staticmethod
    def _retry_after(value):
        try:
            if value.isdecimal():
                return float(value)
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0
