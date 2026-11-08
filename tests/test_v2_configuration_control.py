from copy import deepcopy

import pytest
from test_v2_calibration_control import (
    Delivery,
    base_runtime,
    calibration_request,
    control_module,
    mark_applied,
)
from test_v2_judgment import judgment_configuration


def configuration_request(base):
    request = calibration_request(base)
    request["judgment"] = judgment_configuration()["judgment"]
    request["judgment"]["verification_state"] = "DEFAULT"
    request["judgment"]["fusion"]["verification_state"] = "DEFAULT"
    for c in request["judgment"]["fusion"]["components"]:
        c["weight"] = 0.5
    return request


async def test_all_settings_are_atomic_ack_gated_and_survive_recovery(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    client = Delivery()
    with module.ControlState(base) as state:
        assert hasattr(state, "propose_configuration"), "unified settings control is missing"
        reply = state.propose_configuration(request, "all-settings")
        assert reply["state"] == "PENDING_BACKEND_ACK"
        assert state.snapshot_configuration()["judgment"] is None
        await state.advance(client)
        assert module.effective_runtime(base)["configuration"].get("judgment") is None
        client.acked = True
        await state.advance(client)
        active = state.snapshot_configuration()
        assert active["judgment"] == request["judgment"]
        assert active["calibrations"][0]["calibration"] == request["calibrations"][0]["calibration"]
        active["judgment"]["fusion"]["components"][0]["weight"] = 0.9
        assert (
            state.snapshot_configuration()["judgment"]["fusion"]["components"][0]["weight"] == 0.5
        )
    with module.ControlState(base) as recovered:
        effective = module.effective_runtime(base)
        assert recovered.snapshot_configuration()["judgment"] == request["judgment"]
        assert effective["driver_config_revision"] == base["driver_config_revision"]
        assert effective["configuration"]["sensors"][1]["calibration"] is None


async def test_judgment_only_patch_preserves_installation_and_rejection_is_inactive(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    request.pop("calibrations")
    with module.ControlState(base) as state:
        assert hasattr(state, "propose_configuration")
        state.propose_configuration(request, "judgment-only")
        delivery = Delivery()
        delivery.state = "quarantine"
        await state.advance(delivery)
        assert state.job("judgment-only")["state"] == "REJECTED"
        assert state.snapshot_configuration()["judgment"] is None
        assert all(c["calibration"] is None for c in state.snapshot()["calibrations"])


@pytest.mark.parametrize("change", ["weight", "curve", "sensor", "bins", "driver", "empty"])
def test_bad_unified_settings_never_create_a_pending_transaction(tmp_path, change):
    module = control_module()
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    if change == "weight":
        request["judgment"]["fusion"]["components"][0]["weight"] = 0.8
    elif change == "curve":
        request["judgment"]["fusion"]["components"][0]["calibration"] = [
            [0, 0],
            [0.5, 0.8],
            [1, 0.4],
        ]
    elif change == "sensor":
        request["judgment"]["sections"][0]["sensor_id"] = "unregistered"
    elif change == "bins":
        request["judgment"]["sections"][0]["active_bins"].pop()
    elif change == "driver":
        request["drivers"] = {"lidar-a": "unix:/other.sock"}
    else:
        request.pop("calibrations")
        request.pop("judgment")
    with module.ControlState(base) as state:
        assert hasattr(state, "propose_configuration")
        before = deepcopy(state.state)
        with pytest.raises(ValueError):
            state.propose_configuration(request, "invalid-settings")
        assert state.state == before


def test_legacy_calibration_route_cannot_modify_judgment(tmp_path):
    base = base_runtime(tmp_path)
    with control_module().ControlState(base) as state:
        with pytest.raises(ValueError):
            state.propose(configuration_request(base), "legacy-extra")


async def test_legacy_installation_edit_preserves_judgment_and_generic_null_disables_it(tmp_path):
    base = base_runtime(tmp_path)
    request = configuration_request(base)
    module = control_module()
    with module.ControlState(base) as state:
        delivery = Delivery()
        delivery.acked = True
        state.propose_configuration(request, "all")
        await state.advance(delivery)
        mark_applied(state, base)
        legacy = calibration_request(base)
        legacy.update(base_config_revision="cal-update-1", config_revision="cal-update-2")
        legacy["calibrations"][0]["calibration"].update(
            version="installation-v2", translation_mm=[0, 0, 2000]
        )
        state.propose(legacy, "installation-only")
        await state.advance(delivery)
        assert state.snapshot_configuration()["judgment"] == request["judgment"]
        mark_applied(state, base)
        disable = {k: v for k, v in legacy.items() if k != "calibrations"}
        disable.update(
            base_config_revision="cal-update-2", config_revision="cal-update-3", judgment=None
        )
        state.propose_configuration(disable, "disable")
        await state.advance(delivery)
        assert state.snapshot_configuration()["judgment"] is None
        assert state.snapshot()["calibrations"][0]["calibration"]["translation_mm"] == [0, 0, 2000]
