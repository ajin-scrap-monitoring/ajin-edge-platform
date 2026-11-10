import json
from copy import deepcopy

import httpx
from aiohttp.test_utils import TestClient, TestServer
from ajin_edge.v2.calibration_api import create_app
from ajin_edge.v2.configuration_state import ControlState, effective_runtime
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.point_cloud import DEFAULT_POINT_CLOUD
from ajin_edge.v2.transport import DeliveryClient
from test_v2_calibration_control import Delivery, base_runtime, mark_applied
from test_v2_configuration_control import configuration_request
from test_v2_point_cloud_integration import populated


async def test_point_cloud_settings_use_existing_authenticated_ack_gated_control(tmp_path):
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    request["judgment"]["point_cloud"] = deepcopy(DEFAULT_POINT_CLOUD)
    token = tmp_path / "token"
    credential = "synthetic-control-token-" + "x" * 32
    token.write_text(credential)
    delivery = Delivery()
    with ControlState(base) as state:
        async with TestClient(TestServer(create_app(state, delivery, token))) as client:
            headers = {
                "Authorization": "Bearer " + credential,
                "Idempotency-Key": "cloud-settings",
            }
            assert (await client.put("/api/control/v1/configuration", json=request)).status == 401
            response = await client.put(
                "/api/control/v1/configuration", json=request, headers=headers
            )
            assert response.status == 202
            assert state.snapshot_configuration()["judgment"] is None
            await state.advance(delivery)
            assert effective_runtime(base)["configuration"].get("judgment") is None
            delivery.acked = True
            await state.advance(delivery)
            mark_applied(state, base)
            active = await (
                await client.get("/api/control/v1/configuration", headers=headers)
            ).json()
            assert active["judgment"]["point_cloud"] == request["judgment"]["point_cloud"]
            assert state.job("cloud-settings")["state"] == "APPLIED"


async def test_unsupported_configuration_rejection_preserves_previous_active_settings(tmp_path):
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    request["judgment"]["point_cloud"] = deepcopy(DEFAULT_POINT_CLOUD)
    delivery = Delivery()
    delivery.state = "quarantine"
    with ControlState(base) as state:
        before = deepcopy(state.snapshot_configuration())
        state.propose_configuration(request, "unsupported-cloud")
        await state.advance(delivery)
        assert state.job("unsupported-cloud")["state"] == "REJECTED"
        assert state.snapshot_configuration() == before
        assert "point_cloud" not in json.dumps(effective_runtime(base)["configuration"])


async def test_supported_mock_receives_clouds_on_existing_assessment_route_with_same_retry_body():
    _, orch, _ = populated()
    body = orch.assessment(now_ns=1000000000)
    requests = []

    def receive(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        decoded = json.loads(request.content)
        validate_message(decoded)
        assert request.url.path == "/api/edge/v2/assessments"
        assert request.headers["Idempotency-Key"] == body["message_id"]
        assert [c["point_count"] for c in decoded["filtered_point_clouds"]] == [3, 3]
        return httpx.Response(
            201,
            json={
                "message_id": body["message_id"],
                "accepted": True,
                "duplicate": False,
                "received_at": "2026-09-20T00:00:01.000Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as http:
        transport = DeliveryClient("https://receiver.example", "synthetic-token", http)
        assert (await transport.send(body)).kind == "retry"
        assert (await transport.send(body)).kind == "accepted"
    assert requests[0].content == requests[1].content
