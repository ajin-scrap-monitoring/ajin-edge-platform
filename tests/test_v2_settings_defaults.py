import importlib
from copy import deepcopy

import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.processing import validate_configuration
from test_v2_judgment import judgment_configuration, observation
from test_v2_processing import configuration


def default_profile():
    name = "ajin_edge.v2.settings_defaults"
    assert importlib.util.find_spec(name), "built-in settings profile is missing"
    cfg = configuration()
    cfg["coordinate_frame"]["frame_id"] = "synthetic-scrap-pit-v1"
    return importlib.import_module(name).default_settings(cfg)


def test_simulator_defaults_are_explicit_independent_and_owned():
    profile = default_profile()
    assert profile["profile_id"] == "synthetic-scrap-pit-v1"
    assert profile["calibrations"][0]["calibration"]["translation_mm"] == [500, 2500, 10000]
    assert profile["calibrations"][1]["calibration"]["translation_mm"] == [2300, 3900, 10000]
    model = profile["judgment"]
    assert model["verification_state"] == model["fusion"]["verification_state"] == "DEFAULT"
    assert [c["weight"] for c in model["fusion"]["components"]] == [0.5, 0.5]
    assert all(c["calibration"] == [[0, 0], [1, 1]] for c in model["fusion"]["components"])
    assert model["sections"][0]["axis_xy"] == [1, 0]
    assert model["sections"][1]["axis_xy"] == [0, 1]
    model["fusion"]["components"][0]["weight"] = 0.9
    assert default_profile()["judgment"]["fusion"]["components"][0]["weight"] == 0.5


def test_defaults_cannot_guess_installation_in_an_unknown_coordinate_frame():
    default_profile()
    module = importlib.import_module("ajin_edge.v2.settings_defaults")
    with pytest.raises(ValueError):
        module.default_settings(configuration())


@pytest.mark.parametrize(
    "model_state,fusion_state", [("DEFAULT", "DEFAULT"), ("VERIFIED", "DEFAULT")]
)
def test_default_estimates_are_available_but_never_reported_as_validated(model_state, fusion_state):
    cfg = judgment_configuration()
    cfg["judgment"]["verification_state"] = model_state
    cfg["judgment"]["fusion"]["verification_state"] = fusion_state
    for c in cfg["judgment"]["fusion"]["components"]:
        c["weight"] = 0.5
    validate_configuration(cfg)
    orch = Orchestrator(cfg)
    original = deepcopy(cfg)
    for seq, ms in enumerate([800, 900, 1000], 1):
        for sid, height in [("lidar-a", 200), ("lidar-b", 800)]:
            orch.ingest(observation(cfg, sid, seq, ms, [height] * 5), now_ns=ms * 1000000)
    body = orch.assessment(now_ns=1000000000)
    validate_message(body)
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)
    assert body["decision_state"] == "LIMITED"
    assert "DEFAULT_SETTINGS" in body["reason_codes"]
    assert cfg == original
    if model_state == "DEFAULT":
        assert all(r["state"] == "LIMITED" for r in body["section_profiles"])


def test_unverified_method_still_blocks_default_fusion():
    cfg = judgment_configuration()
    cfg["judgment"]["verification_state"] = "UNVERIFIED"
    cfg["judgment"]["fusion"]["verification_state"] = "DEFAULT"
    validate_configuration(cfg)
    body = Orchestrator(cfg).assessment(now_ns=1000000000)
    assert body["fill_estimate"] is None
    assert "FILL_METHOD_NOT_VALIDATED" in body["reason_codes"]
