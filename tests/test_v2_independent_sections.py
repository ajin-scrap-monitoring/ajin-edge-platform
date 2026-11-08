import math
from copy import deepcopy

import numpy as np
import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.processing import canonical, transform_scan, validate_configuration
from test_v2_judgment import feed, judgment_configuration, legacy_judgment_configuration
from test_v2_processing import scan


def independent_configuration():
    return judgment_configuration()


def tilted_configuration():
    cfg = independent_configuration()
    # Public synthetic simulator environment.v1.json, commit189c0a6. Not field measurements.
    installations = [
        ([500, 2500, 10000], [0, -0.22414769292142694, -0.9745551866148997], [-1, 0, 0]),
        ([2300, 3900, 10000], [0.14834045293024462, 0, -0.9889363528682975], [0, -1, 0]),
    ]
    for sensor, section, (origin, u0, u90), axis in zip(
        cfg["sensors"], cfg["judgment"]["sections"], installations, [[1, 0], [0, 1]], strict=True
    ):
        normal = np.cross(u0, u90)
        sensor["calibration"].update(
            rotation=np.column_stack([u0, u90, normal]).tolist(), translation_mm=origin
        )
        sensor["angle_intervals_mdeg"] = [{"start_inclusive": 0, "end_exclusive": 360000}]
        section.update(
            origin_xy_mm=[1000, 0] if axis[0] else [0, 1000],
            axis_xy=axis,
            bin_width_mm=1000,
            floor_z_mm=[0] * 3,
            ceiling_z_mm=[10000] * 3,
            active_bins=[True] * 3,
        )
    return cfg


def measured_observation(cfg, sensor_index, sequence, ms, height):
    sensor = cfg["sensors"][sensor_index]
    section = cfg["judgment"]["sections"][sensor_index]
    cal = sensor["calibration"]
    rotation, origin = np.asarray(cal["rotation"]), np.asarray(cal["translation_mm"])
    normal = rotation[:, 2]
    axis = np.asarray(section["axis_xy"])
    horizontal_normal = normal[:2] / np.linalg.norm(normal[:2])
    samples = []
    for i in range(len(section["active_bins"])):
        xy = np.asarray(section["origin_xy_mm"]) + axis * (i + 0.5) * section["bin_width_mm"]
        z = section["floor_z_mm"][i] + height
        across = np.dot(normal, origin - np.r_[xy, z]) / np.linalg.norm(normal[:2])
        point = np.r_[xy + across * horizontal_normal, z]
        local = rotation.T @ (point - origin)
        assert abs(local[2]) < 1e-6
        samples.append(
            {
                "angle_mdeg": round(math.degrees(math.atan2(local[1], local[0])) * 1000) % 360000,
                "distance_mm": round(math.hypot(local[0], local[1])),
                "quality": 100,
            }
        )
    raw = scan(sensor["sensor_id"], sequence, ms)
    raw["samples"] = samples
    return transform_scan(raw, sensor)


def test_actual_synthetic_non_coplanar_installations_keep_independent_heights_and_ratios():
    cfg = tilted_configuration()
    orch = Orchestrator(cfg)
    originals = []
    for sequence, ms in enumerate([800, 900, 1000], 1):
        for index, height in enumerate([2000, 8000]):
            obs = measured_observation(cfg, index, sequence, ms, height)
            before = canonical(obs)
            assert orch.ingest(obs, now_ns=ms * 1000000)
            assert canonical(obs) == before
            originals.append(obs)
    body = orch.assessment(now_ns=1000000000)
    validate_message(body)
    profiles = {r["sensor_id"]: r["profile"] for r in body["section_profiles"]}
    assert profiles["lidar-a"]["area_ratio"] == pytest.approx(0.2, abs=0.001)
    assert profiles["lidar-b"]["area_ratio"] == pytest.approx(0.8, abs=0.001)
    assert all(b["height_mm"] == pytest.approx(2000, abs=2) for b in profiles["lidar-a"]["bins"])
    assert all(b["height_mm"] == pytest.approx(8000, abs=2) for b in profiles["lidar-b"]["bins"])
    assert "SENSOR_DISAGREEMENT" not in body["reason_codes"]
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.65, abs=0.001)
    assert body["fusion_result"]["components"][0]["weight"] == 0.25
    assert len(body["evidence"]["scans"]) == len(originals)
    bundle = orch.assessment_bundle(now_ns=1000000000)
    by_id = {o["scan"]["scan_id"]: o for o in originals}
    for message in bundle[:-1]:
        validate_message(message)
        delivered = message["sensors"][0]
        assert canonical(delivered) == canonical(by_id[delivered["scan"]["scan_id"]])


def populate(cfg, *, times=((800, 900, 1000), (800, 900, 1000))):
    orch = Orchestrator(cfg)
    for index, height in enumerate([2000, 8000]):
        for sequence, ms in enumerate(times[index], 1):
            orch.ingest(measured_observation(cfg, index, sequence, ms, height), now_ns=ms * 1000000)
    return orch


def test_each_section_has_its_own_range_width_capacity_mask_and_axis():
    cfg = tilted_configuration()
    cfg["judgment"]["sections"][1].update(
        bin_width_mm=500,
        floor_z_mm=[0] * 4,
        ceiling_z_mm=[10000] * 4,
        active_bins=[True, False, True, True],
    )
    body = populate(cfg).assessment(now_ns=1000000000)
    a, b = [r["profile"] for r in body["section_profiles"]]
    assert a["axis_xy"] == [1, 0] and b["axis_xy"] == [0, 1]
    assert len(a["bins"]) == 3 and len(b["bins"]) == 4
    assert a["capacity_area_mm2"] == 30000000
    assert b["capacity_area_mm2"] == 15000000
    assert b["bins"][1]["source"] == "MASKED"
    assert body["fill_estimate"]["ratio"] == pytest.approx(0.65, abs=0.001)


@pytest.mark.parametrize("times", [((800, 900, 1000), ()), ((600, 650, 700), (900, 950, 1000))])
def test_missing_or_time_mismatched_sensor_preserves_valid_individual_sections_without_fusion(
    times,
):
    body = populate(tilted_configuration(), times=times).assessment(now_ns=1000000000)
    assert body["section_profiles"][0]["state"] == "AVAILABLE"
    assert body["decision_state"] == "LIMITED"
    assert body["fusion_result"] is None and body["fill_estimate"] is None
    reason = "SENSOR_TIME_MISMATCH" if times[1] else "INSUFFICIENT_INPUT"
    assert reason in body["reason_codes"]
    if times[1]:
        assert body["section_profiles"][1]["state"] == "AVAILABLE"
    else:
        assert {r["sensor_id"] for r in body["evidence"]["scans"]} == {"lidar-a"}


def test_unverified_fusion_keeps_sections_without_implicit_average():
    cfg = tilted_configuration()
    cfg["judgment"]["fusion"]["verification_state"] = "UNVERIFIED"
    body = populate(cfg).assessment(now_ns=1000000000)
    assert all(r["state"] == "AVAILABLE" for r in body["section_profiles"])
    assert body["fill_estimate"] is None and body["fusion_result"] is None
    assert "FILL_FUSION_NOT_VALIDATED" in body["reason_codes"]


def test_distinct_clock_domains_keep_each_section_but_cannot_fuse_identical_numbers():
    cfg = tilted_configuration()
    orch = Orchestrator(cfg)
    for index in range(2):
        for sequence, ms in enumerate([800, 900, 1000], 1):
            obs = measured_observation(cfg, index, sequence, ms, 2000)
            if index == 1:
                obs["scan"]["clock_domain_id"] = "other-boot"
            orch.ingest(obs, now_ns=ms * 1000000)
    body = orch.assessment(now_ns=1000000000)
    assert all(r["state"] == "AVAILABLE" for r in body["section_profiles"])
    assert body["reason_codes"] == ["SENSOR_TIME_MISMATCH"]
    assert body["fill_estimate"] is None and len(body["evidence"]["scans"]) == 6


@pytest.mark.parametrize("fusion_state", ["VERIFIED", "UNVERIFIED", "ABSENT"])
def test_older_matching_pair_cannot_truncate_current_sections_or_hide_current_skew(fusion_state):
    cfg = tilted_configuration()
    if fusion_state == "ABSENT":
        cfg["judgment"]["fusion"] = None
    else:
        cfg["judgment"]["fusion"]["verification_state"] = fusion_state
    body = populate(cfg, times=((800, 900, 1000), (600, 650, 700))).assessment(now_ns=1000000000)
    assert all(r["state"] == "AVAILABLE" for r in body["section_profiles"])
    assert all(r["profile"]["sensor_stats"][0]["scan_count"] == 3 for r in body["section_profiles"])
    assert len(body["evidence"]["scans"]) == 6
    assert "SENSOR_TIME_MISMATCH" in body["reason_codes"]
    assert body["fill_estimate"] is None and body["fusion_result"] is None


@pytest.mark.parametrize("delta,available", [(100, True), (101, False)])
def test_fusion_uses_latest_individual_anchors_at_exact_tolerance_boundary(delta, available):
    cfg = tilted_configuration()
    body = populate(
        cfg, times=((800, 900, 1000), (800 - delta, 900 - delta, 1000 - delta))
    ).assessment(now_ns=1000000000)
    assert all(r["state"] == "AVAILABLE" for r in body["section_profiles"])
    assert (body["fusion_result"] is not None) == available
    if available:
        assert body["fusion_result"]["pair_delta_ms"] == 100
        assert all(
            c["anchor_scan_id"] == f"{c['sensor_id']}-3"
            for c in body["fusion_result"]["components"]
        )
    else:
        assert "SENSOR_TIME_MISMATCH" in body["reason_codes"]


def test_legacy_shared_model_is_registered_but_cannot_emit_numeric_judgment():
    cfg = legacy_judgment_configuration()
    validate_configuration(cfg)
    orch = Orchestrator(cfg)
    feed(orch, cfg)
    body = orch.assessment(now_ns=1000000000)
    assert body["fill_estimate"] is None and body["evidence"]["scans"] == []
    assert body["reason_codes"] == ["LEGACY_SECTION_MODEL_UNSUPPORTED"]


def test_axis_outside_own_scan_plane_keeps_other_section_available():
    cfg = tilted_configuration()
    # Construct valid sensor measurements first, then change the section axis only.
    original = deepcopy(cfg)
    cfg["judgment"]["sections"][0]["axis_xy"] = [0, 1]
    orch = Orchestrator(cfg)
    for i in range(2):
        for seq, ms in enumerate([800, 900, 1000], 1):
            orch.ingest(measured_observation(original, i, seq, ms, 2000), now_ns=ms * 1000000)
    body = orch.assessment(now_ns=1000000000)
    assert body["section_profiles"][0]["reason_codes"] == ["SECTION_GEOMETRY_MISMATCH"]
    assert body["section_profiles"][1]["state"] == "AVAILABLE"
    assert body["fill_estimate"] is None


@pytest.mark.parametrize("change", ["weights", "zero", "duplicate", "sensor", "curve"])
def test_fusion_requires_explicit_consistent_sensor_weights_and_curves(change):
    cfg = tilted_configuration()
    components = cfg["judgment"]["fusion"]["components"]
    if change == "weights":
        components[0]["weight"] = 0.3
    elif change == "zero":
        components[0]["weight"], components[1]["weight"] = 0, 1
    elif change == "duplicate":
        components[1]["sensor_id"] = "lidar-a"
    elif change == "sensor":
        cfg["judgment"]["sections"][1]["sensor_id"] = "unregistered"
    else:
        components[0]["calibration"] = [[0, 0], [0.5, 0.8], [1, 0.7]]
    with pytest.raises(ValueError):
        validate_configuration(cfg)


@pytest.mark.parametrize(
    "change", ["owner", "ratio", "anchor", "weight", "mixed", "stats", "plane"]
)
def test_contract_rejects_inconsistent_independent_results(change):
    body = populate(tilted_configuration()).assessment(now_ns=1000000000)
    profile = body["section_profiles"][0]["profile"]
    if change == "owner":
        profile["bins"][0]["sensor_ids"] = ["lidar-b"]
    elif change == "ratio":
        body["fusion_result"]["components"][0]["section_ratio"] = 0.4
    elif change == "anchor":
        body["fusion_result"]["components"][0]["anchor_scan_id"] = "absent"
    elif change == "weight":
        body["fusion_result"]["components"][0]["weight"] = 0.4
    elif change == "mixed":
        body["section_profile"] = None
    elif change == "stats":
        profile["sensor_stats"][0]["scan_count"] += 1
    else:
        profile["plane_normal"] = [0, 0, 1]
    with pytest.raises(ValueError):
        validate_message(body)
