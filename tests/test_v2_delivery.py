import json

import httpx
import pytest
from ajin_edge.v2.transport import Backoff, DeliveryClient


def payload():
    return {"message_id": "message-test", "message_type": "lidar_measurement"}


@pytest.mark.parametrize(
    "code,body,kind",
    [
        (
            201,
            {
                "message_id": "message-test",
                "accepted": True,
                "duplicate": False,
                "received_at": "2026-09-20T01:00:00Z",
            },
            "accepted",
        ),
        (
            200,
            {
                "message_id": "message-test",
                "accepted": True,
                "duplicate": True,
                "received_at": "2026-09-20T01:00:00Z",
            },
            "accepted",
        ),
        (200, {"message_id": "wrong", "accepted": True}, "retry"),
        (201, {"message_id": "message-test", "accepted": True}, "retry"),
        (409, {"duplicate": True}, "quarantine"),
        (400, {}, "quarantine"),
        (422, {}, "quarantine"),
        (413, {}, "quarantine"),
        (401, {}, "auth"),
        (403, {}, "auth"),
        (503, {}, "retry"),
        (408, {}, "retry"),
        (429, {}, "retry"),
        (302, {}, "configuration"),
        (404, {}, "configuration"),
        (405, {}, "configuration"),
        (415, {}, "configuration"),
    ],
)
async def test_ack_and_error_classification(code, body, kind):
    async def handler(request):
        assert str(request.url) == "https://backend.example/api/edge/v2/measurements"
        assert request.headers["Idempotency-Key"] == "message-test"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "Content-Encoding" not in request.headers
        assert json.loads(request.content) == payload()
        return httpx.Response(code, json=body, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DeliveryClient("https://backend.example", "test-token", http).send(payload())
    assert result.kind == kind
    if code == 429:
        assert result.retry_after == 120


async def test_total_timeout_includes_slow_ack():
    import asyncio

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(201, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DeliveryClient("https://backend.example", "test-token", http, timeout=0.01)
        assert (await client.send(payload())).kind == "retry"


def test_backoff_caps_jitter_and_success_resets():
    backoff = Backoff(random=lambda: 1)
    delays = [backoff.failure() for _ in range(10)]
    assert delays[:5] == [1.2, 2.4, 4.8, 9.6, 19.2]
    assert max(delays) == 30
    backoff.success()
    assert backoff.failure() == 1.2


@pytest.mark.parametrize(
    "url",
    [
        "http://backend.example",
        "https://u:p@backend.example",
        "https://backend.example?token=secret",
        "https://backend.example/#fragment",
    ],
)
def test_unsafe_base_url_rejected(url):
    with pytest.raises(ValueError):
        DeliveryClient(url, "test-token", None)
