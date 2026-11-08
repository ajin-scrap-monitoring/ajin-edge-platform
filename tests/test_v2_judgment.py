import math
from copy import deepcopy

import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.processing import (
    ProcessingEngine,
    canonical,
    transform_scan,
    validate_configuration,
)
from test_v2_processing import configuration, scan


def judgment_configuration():
    cfg = configuration()
    for s in cfg["sensors"]:
        s["calibration"]["translation_mm"] = [0, 0, 0]
    cfg["judgment"] = {
        "method_version": "synthetic-section-v1",
        "verification_state": "VERIFIED",
        "section": {
            "origin_xy_mm": [0, 0],
            "axis_xy": [1, 0],
            "plane_tolerance_mm": 10,
            "bin_width_mm": 50,
            "floor_z_mm": [0] * 5,
            "ceiling_z_mm": [1000] * 5,
            "active_bins": [True] * 5,
        },
        "filters": {
            "quality_min": 10,
            "distance_min_mm": 1,
            "distance_max_mm": 30000,
            "height_margin_mm": 30,
            "spatial_radius_bins": 0,
            "spatial_max_deviation_mm": 200,
        },
        "window": {"scan_count": 5, "min_scans": 3, "max_age_ms": 1000, "pair_tolerance_ms": 100},
        "max_interpolation_gap_bins": 1,
        "max_sensor_disagreement_mm": 200,
        "fill_calibration": [[0, 0], [1, 1]],
    }
    return cfg


def observation(cfg, sensor, sequence, ms, heights=None, qualities=None, domain="boot-test"):
    raw = scan(sensor, sequence, ms, domain)
    heights = [500] * 5 if heights is None else heights
    qualities = [100] * len(heights) if qualities is None else qualities
    raw["samples"] = [
        {
            "angle_mdeg": round(math.degrees(math.atan2(z, 25 + i * 50)) * 1000),
            "distance_mm": round(math.hypot(z, 25 + i * 50)),
            "quality": qualities[i],
        }
        for i, z in enumerate(heights)
    ]
    return transform_scan(raw, next(s for s in cfg["sensors"] if s["sensor_id"] == sensor))


def feed(orch, cfg, heights=None, qualities=None):
    originals = []
    for seq, ms in enumerate([800, 900, 1000], 1):
        for sensor in ["lidar-a", "lidar-b"]:
            obs = observation(cfg, sensor, seq, ms, heights, qualities)
            before = canonical(obs)
            assert orch.ingest(obs, now_ns=ms * 1000000)
            assert canonical(obs) == before
            originals.append(deepcopy(obs))
    return originals


def test_mathematical_section_area_and_fill_keep_original_observations():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    original_cfg = deepcopy(cfg)
    originals = feed(orch, cfg)
    body = orch.assessment(now_ns=1000000000, generated_at="2026-09-20T00:00:01Z")
    validate_message(body)
    assert cfg == original_cfg
    assert body["decision_state"] == "AVAILABLE"
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)
    profile = body["section_profile"]
    assert profile["area_ratio"] == pytest.approx(0.5, abs=0.001)
    assert profile["capacity_area_mm2"] == 250000
    assert profile["area_mm2"] == pytest.approx(125000, abs=250)
    assert len(body["evidence"]["scans"]) == 6
    assert all(b["source"] == "OBSERVED" for b in profile["bins"])
    assert originals[0]["scan"]["points"][0]["quality"] == 100


def result(orch):
    return orch.assessment(now_ns=1000000000, generated_at="2026-09-20T00:00:01Z")


def test_low_quality_is_only_filtered_in_judgment_and_short_interior_gap_is_interpolated():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    original = feed(orch, cfg, qualities=[100, 100, 0, 100, 100])
    body = result(orch)
    assert body["decision_state"] == "LIMITED"
    assert body["section_profile"]["bins"][2]["source"] == "INTERPOLATED"
    assert body["section_profile"]["coverage_ratio"] == 0.8
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)
    bundle = orch.assessment_bundle(now_ns=1000000000)
    assert len(bundle) == 7
    assert all(x["delivery_kind"] == "EVIDENCE" for x in bundle[:-1])
    by_id = {o["scan"]["scan_id"]: o for o in original}
    for payload in bundle[:-1]:
        source = by_id[payload["sensors"][0]["scan"]["scan_id"]]
        validate_message(payload)
        assert payload["sensors"][0] == source
        assert payload["sensors"][0]["scan"]["points"][2]["quality"] == 0
        assert payload["assessment_ids"] == [bundle[-1]["assessment_id"]]


def test_temporal_median_rejects_transient_spike_and_owns_immutable_input_copy():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    for seq, ms in enumerate([800, 900, 1000], 1):
        for sid in ["lidar-a", "lidar-b"]:
            heights = [500, 500, 900 if seq == 3 else 500, 500, 500]
            obs = observation(cfg, sid, seq, ms, heights)
            orch.ingest(obs, now_ns=ms * 1000000)
            obs["scan"]["points"].clear()
    assert result(orch)["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)


def test_spatial_median_removes_isolated_bin_spike_before_interpolation():
    cfg = judgment_configuration()
    cfg["judgment"]["filters"]["spatial_radius_bins"] = 1
    orch = Orchestrator(cfg)
    feed(orch, cfg, [500, 500, 900, 500, 500])
    body = result(orch)
    assert body["section_profile"]["bins"][2]["source"] == "INTERPOLATED"
    assert all(s["spatial_rejected_bins"] == 3 for s in body["section_profile"]["sensor_stats"])
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)


@pytest.mark.parametrize(
    "qualities,active",
    [
        ([0, 100, 100, 100, 100], [True] * 5),
        ([100, 0, 0, 100, 100], [True] * 5),
        ([100, 100, 100, 0, 100], [True, True, False, True, True]),
    ],
)
def test_never_extrapolates_or_bridges_long_gaps_or_masks(qualities, active):
    cfg = judgment_configuration()
    cfg["judgment"]["section"]["active_bins"] = active
    orch = Orchestrator(cfg)
    feed(orch, cfg, qualities=qualities)
    body = result(orch)
    assert body["fill_estimate"] is None
    assert body["section_profile"]["area_ratio"] is None
    assert "SECTION_INCOMPLETE" in body["reason_codes"]


def test_ab_disagreement_is_not_interpolated_away():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    for seq, ms in enumerate([800, 900, 1000], 1):
        for sid in ["lidar-a", "lidar-b"]:
            orch.ingest(
                observation(
                    cfg, sid, seq, ms, [500, 500, 900 if sid == "lidar-b" else 500, 500, 500]
                ),
                now_ns=ms * 1000000,
            )
    body = result(orch)
    assert body["section_profile"]["bins"][2]["source"] == "CONFLICT"
    assert body["fill_estimate"] is None
    assert "SENSOR_DISAGREEMENT" in body["reason_codes"]


def test_ab_complementary_sections_are_fused_in_common_coordinates():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    for seq, ms in enumerate([800, 900, 1000], 1):
        for sid, qualities in [
            ("lidar-a", [100, 100, 100, 0, 0]),
            ("lidar-b", [0, 0, 100, 100, 100]),
        ]:
            orch.ingest(observation(cfg, sid, seq, ms, qualities=qualities), now_ns=ms * 1000000)
    body = result(orch)
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)
    assert body["section_profile"]["bins"][0]["sensor_ids"] == ["lidar-a"]
    assert body["section_profile"]["bins"][4]["sensor_ids"] == ["lidar-b"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("method", "FILL_METHOD_NOT_VALIDATED"),
        ("missing", "CALIBRATION_MISSING"),
        ("unverified", "CALIBRATION_UNVERIFIED"),
    ],
)
def test_unverified_or_missing_settings_never_emit_numeric_judgment(change, reason):
    cfg = judgment_configuration()
    if change == "method":
        cfg["judgment"]["verification_state"] = "UNVERIFIED"
    elif change == "missing":
        cfg["sensors"][0]["calibration"] = None
    else:
        cfg["sensors"][0]["calibration"]["verification_state"] = "UNVERIFIED"
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    body = result(orch)
    assert body["fill_estimate"] is None and body["section_profile"] is None
    assert reason in body["reason_codes"]
    assert body["evidence"]["scans"] == []


def test_section_ratio_is_not_automatically_a_volume_fill_ratio():
    cfg = judgment_configuration()
    cfg["judgment"]["fill_calibration"] = None
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    body = result(orch)
    assert body["section_profile"]["area_ratio"] == pytest.approx(0.5, abs=0.001)
    assert body["fill_estimate"] is None
    assert body["capabilities"]["fill"] == "NOT_VALIDATED"
    cfg["judgment"]["fill_calibration"] = [[0, 0], [0.5, 0.3], [1, 1]]
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    assert result(orch)["fill_estimate"]["ratio"] == pytest.approx(0.3, abs=0.001)


def test_expired_scans_stream_restart_and_clock_mismatch_are_not_mixed():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    assert orch.assessment(now_ns=2000000001)["fill_estimate"] is None
    new = observation(cfg, "lidar-a", 4, 1100)
    new["scan"]["stream_instance_id"] = "new-stream"
    orch.ingest(new, now_ns=1100000000)
    assert orch.assessment(now_ns=1100000000)["reason_codes"] == ["INSUFFICIENT_HISTORY"]
    other = observation(cfg, "lidar-b", 4, 1100, domain="other-boot")
    orch.ingest(other, now_ns=1100000000)
    assert orch.assessment(now_ns=1100000000)["reason_codes"] == ["SENSOR_TIME_MISMATCH"]


def test_calculation_expiration_is_checked_again_before_publication(monkeypatch):
    import ajin_edge.v2.orchestration as module

    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    values = iter([1100000000, 1900000000])
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(values))
    body = orch.assessment()
    assert body["fill_estimate"] is None
    assert body["reason_codes"] == ["INPUT_EXPIRED_DURING_JUDGMENT"]


@pytest.mark.parametrize("change", ["length", "axis", "curve", "window", "nan"])
def test_invalid_judgment_settings_are_rejected(change):
    cfg = judgment_configuration()
    model = cfg["judgment"]
    if change == "length":
        model["section"]["active_bins"].pop()
    elif change == "axis":
        model["section"]["axis_xy"] = [1, 1]
    elif change == "curve":
        model["fill_calibration"] = [[0, 1], [1, 0]]
    elif change == "window":
        model["window"]["min_scans"] = 6
    else:
        model["section"]["plane_tolerance_mm"] = float("nan")
    with pytest.raises(ValueError):
        validate_configuration(cfg)


def test_public_contract_rejects_inconsistent_section_totals():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    body = result(orch)
    body["section_profile"]["area_mm2"] += 100
    with pytest.raises(ValueError):
        validate_message(body)


def test_mutating_a_result_cannot_change_future_geometry():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    body = result(orch)
    body["section_profile"]["axis_xy"][:] = [0, 1]
    body["section_profile"]["origin_xy_mm"][:] = [100, 100]
    assert result(orch)["fill_estimate"]["ratio"] == pytest.approx(0.5, abs=0.001)


def test_periodic_original_delivery_is_identical_with_and_without_judgment():
    cfg = judgment_configuration()
    plain = deepcopy(cfg)
    del plain["judgment"]
    original = ProcessingEngine(plain, clock_domain_id="boot-test")
    with_judgment = ProcessingEngine(cfg, clock_domain_id="boot-test")
    orch = Orchestrator(cfg)
    for sequence, ms in enumerate([800, 900, 1000], 1):
        for sid in ["lidar-a", "lidar-b"]:
            raw = scan(sid, sequence, ms)
            snapshot = canonical(raw)
            original.ingest(raw)
            obs = with_judgment.ingest(raw)
            orch.ingest(obs, now_ns=ms * 1000000)
            assert canonical(raw) == snapshot
    orch.assessment(now_ns=1000000000)
    a = original.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01Z")
    b = with_judgment.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01Z")
    del a["message_id"], b["message_id"]
    assert canonical(a) == canonical(b)


@pytest.mark.parametrize(
    "path",
    [("window", "scan_count"), ("filters", "spatial_radius_bins"), ("section", "bin_width_mm")],
)
def test_integer_parameters_reject_floats_before_constructing_runtime(path):
    cfg = judgment_configuration()
    model = cfg["judgment"]
    model[path[0]][path[1]] = float(model[path[0]][path[1]])
    with pytest.raises(ValueError):
        validate_configuration(cfg)


def test_event_does_not_inherit_unrelated_section_or_fill_evidence(monkeypatch):
    import ajin_edge.v2.orchestration as module

    cfg = judgment_configuration()
    orch = Orchestrator(cfg, enabled_rules=("synthetic-occlusion-v1",))
    originals = feed(orch, cfg)
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: 1000000000)
    event = orch.event(
        "OCCLUSION", "SUSPECTED", [originals[0]], rule_version="synthetic-occlusion-v1"
    )
    assert event["fill_estimate"] is None
    assert event.get("section_profile") is None
    assert len(event["evidence"]["scans"]) == 1
    validate_message(event)
