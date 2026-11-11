"""v2 stored ACK protocol. No v1 409-as-success compatibility."""

import asyncio
import json
import random
import time
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
        self.metrics = {}

    async def send(self, payload):
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(self._send(payload), self.timeout)
        except (TimeoutError, httpx.HTTPError):
            result = Result("retry", "NETWORK_OR_TIMEOUT")
        kind = (
            payload.get("delivery_kind")
            or payload.get("assessment_type")
            or payload["message_type"]
        )
        if kind not in ("PERIODIC", "EVIDENCE", "STATE", *PATHS):
            kind = "OTHER"
        elapsed = (time.monotonic() - started) * 1000
        metric = self.metrics.setdefault(
            kind, {"calls": 0, "elapsed_ms": 0.0, "max_elapsed_ms": 0.0, "results": {}}
        )
        metric["calls"] += 1
        metric["elapsed_ms"] += elapsed
        metric["max_elapsed_ms"] = max(metric["max_elapsed_ms"], elapsed)
        outcome = result.reason or result.kind
        metric["results"][outcome] = metric["results"].get(outcome, 0) + 1
        if result.kind == "accepted" and kind in ("PERIODIC", "STATE"):
            generated = payload.get("generated_at")
            if generated is not None and (
                "latest_accepted_generated_at" not in metric
                or datetime.fromisoformat(generated)
                >= datetime.fromisoformat(metric["latest_accepted_generated_at"])
            ):
                metric["latest_accepted_generated_at"] = generated
                metric["latest_accepted_observed_at"] = datetime.now(UTC).isoformat()
        if result.kind == "accepted" and kind == "PERIODIC":
            sensors = metric.setdefault("latest_accepted_sensors", {})
            for sensor in payload.get("sensors", []):
                scan = sensor.get("scan")
                if scan is None:
                    continue
                sid = sensor["sensor_id"]
                acquired = scan["acquired_at"]
                if sid not in sensors or datetime.fromisoformat(acquired) >= datetime.fromisoformat(
                    sensors[sid]["acquired_at"]
                ):
                    sensors[sid] = {
                        "acquired_at": acquired,
                        "generated_at": payload["generated_at"],
                        "observed_at": datetime.now(UTC).isoformat(),
                    }
        return result

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
