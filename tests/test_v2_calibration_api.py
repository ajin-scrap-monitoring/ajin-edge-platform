import importlib

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_v2_calibration_control import Delivery, base_runtime, calibration_request, control_module


def api_module():
    name = "ajin_edge.v2.calibration_api"
    assert importlib.util.find_spec(name) is not None, "calibration HTTP API is missing"
    return importlib.import_module(name)


async def test_authenticated_put_is_pending_and_never_changes_unacked_configuration(tmp_path):
    api = api_module()
    base = base_runtime(tmp_path)
    token_file = tmp_path / "control-token"
    token_file.write_text("t" * 40)
    with control_module().ControlState(base) as state:
        async with TestClient(TestServer(api.create_app(state, Delivery(), token_file))) as client:
            headers = {"Authorization": "Bearer " + "t" * 40, "Idempotency-Key": "request-1"}
            response = await client.put(
                "/api/control/v1/calibrations", json=calibration_request(base), headers=headers
            )
            assert response.status == 202
            assert (await response.json())["state"] == "PENDING_BACKEND_ACK"
            response = await client.get(
                "/api/control/v1/calibration-jobs/request-1", headers=headers
            )
            assert response.status == 200
            assert (await response.json())["state"] == "PENDING_BACKEND_ACK"
            response = await client.get("/api/control/v1/calibrations", headers=headers)
            data = await response.json()
            assert data["config_revision"] == base["configuration"]["config_revision"]
            assert "backend_origin" not in data and "token_file" not in data


@pytest.mark.parametrize("token", [None, "wrong"])
async def test_unauthorized_request_cannot_create_a_job_or_echo_credentials(tmp_path, token):
    api = api_module()
    base = base_runtime(tmp_path)
    token_file = tmp_path / "control-token"
    token_file.write_text("t" * 40)
    with control_module().ControlState(base) as state:
        async with TestClient(TestServer(api.create_app(state, Delivery(), token_file))) as client:
            headers = {"Idempotency-Key": "request-1"}
            if token:
                headers["Authorization"] = "Bearer " + token
            response = await client.put(
                "/api/control/v1/calibrations", json=calibration_request(base), headers=headers
            )
            assert response.status == 401
            assert "t" * 40 not in await response.text()
            assert state.state["pending"] is None


async def test_invalid_body_limits_encoding_and_unrecognized_fields_are_rejected(tmp_path):
    api = api_module()
    base = base_runtime(tmp_path)
    token_file = tmp_path / "control-token"
    token_file.write_text("t" * 40)
    headers = {
        "Authorization": "Bearer " + "t" * 40,
        "Idempotency-Key": "request-1",
        "Content-Type": "application/json",
    }
    with control_module().ControlState(base) as state:
        async with TestClient(TestServer(api.create_app(state, Delivery(), token_file))) as client:
            for body, expected in [
                (b"x" * 33000, 413),
                (b'{"site_id":"a","site_id":"b"}', 422),
                (b'{"value":NaN}', 422),
            ]:
                response = await client.put(
                    "/api/control/v1/calibrations", data=body, headers=headers
                )
                assert response.status == expected
            response = await client.put(
                "/api/control/v1/calibrations",
                data=b"{}",
                headers={**headers, "Content-Encoding": "gzip"},
            )
            assert response.status == 415
            request = calibration_request(base)
            request["backend_origin"] = "https://other.invalid"
            response = await client.put(
                "/api/control/v1/calibrations", json=request, headers=headers
            )
            assert response.status == 422
            assert state.state["pending"] is None


async def test_control_credential_rotation_does_not_require_restart(tmp_path):
    api = api_module()
    base = base_runtime(tmp_path)
    token_file = tmp_path / "control-token"
    token_file.write_text("a" * 40)
    with control_module().ControlState(base) as state:
        async with TestClient(TestServer(api.create_app(state, Delivery(), token_file))) as client:
            token_file.write_text("b" * 40)
            for value, status in [("a" * 40, 401), ("b" * 40, 200)]:
                response = await client.get(
                    "/api/control/v1/calibrations", headers={"Authorization": "Bearer " + value}
                )
                assert response.status == status


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "example.com"])
def test_listener_rejects_public_wildcard_and_dns_bindings(tmp_path, host):
    api = api_module()
    base = {
        "token_file": str(tmp_path / "backend-token"),
        "calibration_api": {"host": host, "token_file": str(tmp_path / "control-token")},
    }
    (tmp_path / "control-token").write_text("a" * 40)
    with pytest.raises(ValueError):
        api.listener(base)


def test_listener_rejects_shared_or_weak_credentials(tmp_path):
    api = api_module()
    token = tmp_path / "token"
    token.write_text("a" * 40)
    config = {"token_file": str(token), "calibration_api": {"token_file": str(token)}}
    with pytest.raises(ValueError):
        api.listener(config)
    config["token_file"] = str(tmp_path / "backend-token")
    token.write_text("short")
    with pytest.raises(ValueError):
        api.listener(config)
