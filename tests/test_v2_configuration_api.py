import json

from aiohttp.test_utils import TestClient, TestServer
from test_v2_calibration_api import api_module
from test_v2_calibration_control import Delivery, base_runtime, control_module
from test_v2_configuration_control import configuration_request


async def test_configuration_routes_expose_defaults_and_ack_gated_updates(tmp_path):
    base = base_runtime(tmp_path)
    base["configuration"]["coordinate_frame"]["frame_id"] = "synthetic-scrap-pit-v1"
    token = tmp_path / "token"
    token.write_text("t" * 40)
    headers = {"Authorization": "Bearer " + "t" * 40, "Idempotency-Key": "settings-1"}
    with control_module().ControlState(base) as state:
        async with TestClient(
            TestServer(api_module().create_app(state, Delivery(), token))
        ) as client:
            response = await client.get("/api/control/v1/configuration/defaults", headers=headers)
            assert response.status == 200
            defaults = await response.json()
            assert defaults["judgment"]["fusion"]["components"][0]["weight"] == 0.5
            assert defaults["judgment"]["verification_state"] == "DEFAULT"
            response = await client.get("/api/control/v1/configuration", headers=headers)
            assert response.status == 200
            assert (await response.json())["judgment"] is None
            response = await client.put(
                "/api/control/v1/configuration", json=configuration_request(base), headers=headers
            )
            assert response.status == 202
            response = await client.get(
                "/api/control/v1/configuration-jobs/settings-1", headers=headers
            )
            assert response.status == 200
            assert (await response.json())["state"] == "PENDING_BACKEND_ACK"
            response = await client.get("/api/control/v1/configuration")
            assert response.status == 401


async def test_valid_long_section_settings_are_not_limited_by_legacy_calibration_body_size(
    tmp_path,
):
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    for section in request["judgment"]["sections"]:
        section.update(
            floor_z_mm=[-999999999.123456] * 512,
            ceiling_z_mm=[999999999.123456] * 512,
            active_bins=[True] * 512,
        )
    assert len(json.dumps(request).encode()) > 33000
    token = tmp_path / "token"
    token.write_text("t" * 40)
    headers = {"Authorization": "Bearer " + "t" * 40, "Idempotency-Key": "long-settings"}
    with control_module().ControlState(base) as state:
        async with TestClient(
            TestServer(api_module().create_app(state, Delivery(), token))
        ) as client:
            response = await client.put(
                "/api/control/v1/configuration", json=request, headers=headers
            )
            assert response.status == 202, await response.text()
