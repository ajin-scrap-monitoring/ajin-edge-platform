import importlib
import math
from copy import deepcopy

import pytest
from ajin_edge.v2.processing import canonical, transform_scan
from test_v2_judgment import judgment_configuration
from test_v2_processing import scan


def point_cloud_settings():
    return {
        "enabled": True,
        "processing_version": "ANGULAR_RANGE_MEDIAN_V1",
        "quality_min": 10,
        "distance_min_mm": 1,
        "distance_max_mm": 30000,
        "spatial_radius_points": 2,
        "spatial_min_neighbors": 3,
        "spatial_max_deviation_mm": 200,
        "spatial_neighbor_spread_mm": 100,
        "max_adjacent_angle_gap_mdeg": 500,
        "angle_match_tolerance_mdeg": 50,
    }


def cloud_configuration():
    cfg = judgment_configuration()
    cfg["judgment"]["point_cloud"] = point_cloud_settings()
    for s in cfg["sensors"]:
        s["calibration"]["translation_mm"] = [10, 20, 30]
    return cfg


def point_observation(cfg, seq, ms, distances, *, angles=None, sid="lidar-a", quality=None):
    raw = scan(sid, seq, ms)
    raw["samples"] = [
        {"angle_mdeg": a, "distance_mm": d, "quality": 100 if quality is None else quality[i]}
        for i, (a, d) in enumerate(
            zip(angles or range(0, len(distances) * 100, 100), distances, strict=True)
        )
    ]
    sensor = next(s for s in cfg["sensors"] if s["sensor_id"] == sid)
    return transform_scan(raw, sensor)


def calculate(cfg, observations, *, now_ns=1000000000, sensor_index=0):
    module = importlib.import_module("ajin_edge.v2.point_cloud")
    return module.build_cloud(cfg, cfg["sensors"][sensor_index], observations, now_ns=now_ns)


def test_full_anchor_array_and_range_median_preserve_raw_and_do_not_reapply_world_pose():
    cfg = cloud_configuration()
    cfg["judgment"]["point_cloud"]["spatial_radius_points"] = 0
    obs = [
        point_observation(cfg, i, ms, [d] * 7)
        for i, (ms, d) in enumerate([(800, 1000), (900, 1100), (1000, 1200)], 1)
    ]
    before = canonical(obs)
    result = calculate(cfg, obs)
    assert canonical(obs) == before
    assert result["point_count"] == result["valid_point_count"] == 7
    assert result["point_indices"] == list(range(7))
    assert result["xyz_mm"][0] == [1110.0, 20.0, 30.0]
    assert result["xyz_mm"][6][2] == pytest.approx(
        30 + 1100 * math.sin(math.radians(0.6)), abs=0.001
    )
    assert result["sample_counts"] == [3] * 7
    assert all(result["valid"])


def test_isolated_spike_is_null_but_discontinuous_surface_edge_is_preserved():
    cfg = cloud_configuration()
    spike = [1000, 1000, 1000, 5000, 1000, 1000, 1000]
    observations = [
        point_observation(cfg, i, ms, spike) for i, ms in enumerate([800, 900, 1000], 1)
    ]
    r = calculate(cfg, observations)
    assert r["xyz_mm"][3] is None and not r["valid"][3]
    assert r["invalid_reason_codes"][3] == ["SPATIAL_OUTLIER"]
    edge = [1000, 1000, 1000, 2000, 2000, 2000, 2000]
    r = calculate(
        cfg, [point_observation(cfg, i, ms, edge) for i, ms in enumerate([800, 900, 1000], 1)]
    )
    assert all(r["valid"])


@pytest.mark.parametrize("reason", ["LOW_QUALITY", "OUT_OF_RANGE", "SDK_INVALID_RANGE"])
def test_bad_anchor_is_not_repaired_from_good_history(reason):
    cfg = cloud_configuration()
    obs = [point_observation(cfg, i, ms, [1000] * 7) for i, ms in enumerate([800, 900, 1000], 1)]
    p = obs[-1]["scan"]["points"][3]
    if reason == "LOW_QUALITY":
        p["quality"] = 0
    elif reason == "OUT_OF_RANGE":
        p["distance_mm"] = 0
    else:
        p["calculation_reason_codes"] = [reason]
        p["position_mm"] = None
    r = calculate(cfg, obs)
    assert len(r["xyz_mm"]) == 7 and r["xyz_mm"][3] is None
    assert r["invalid_reason_codes"][3] == [reason]
    assert r["sample_counts"][3] == 0


def test_angular_correspondence_is_one_to_one_with_deterministic_ties():
    cfg = cloud_configuration()
    cfg["judgment"]["point_cloud"]["spatial_radius_points"] = 0
    obs = [point_observation(cfg, i, ms, [1000], angles=[50]) for i, ms in enumerate([800, 900], 1)]
    obs.append(point_observation(cfg, 3, 1000, [1000, 1000], angles=[0, 100]))
    r = calculate(cfg, obs)
    assert r["sample_counts"] == [3, 1]
    assert r["valid"] == [True, False]
    assert r["invalid_reason_codes"][1] == ["INSUFFICIENT_HISTORY"]


def test_angle_interval_gap_is_not_a_spatial_neighbor_or_temporal_match():
    cfg = cloud_configuration()
    cfg["sensors"][0]["angle_intervals_mdeg"] = [
        {"start_inclusive": 0, "end_exclusive": 150},
        {"start_inclusive": 200, "end_exclusive": 450},
    ]
    obs = [
        point_observation(cfg, i, ms, [1000, 1000, 9000, 1000, 1000])
        for i, ms in enumerate([800, 900, 1000], 1)
    ]
    r = calculate(cfg, obs)
    assert r["valid_point_count"] == 5  # No three coherent neighbors in either interval.


def test_expiry_boundary_and_stream_change_do_not_reuse_old_points():
    cfg = cloud_configuration()
    obs = [point_observation(cfg, i, ms, [1000]) for i, ms in enumerate([0, 100, 200], 1)]
    assert calculate(cfg, obs)["valid"] == [True]
    assert calculate(cfg, obs, now_ns=1000000001)["valid"] == [False]
    changed = deepcopy(obs)
    changed[-1]["scan"]["stream_instance_id"] = "new-stream"
    assert calculate(cfg, changed)["sample_counts"] == [1]


def test_no_cross_sensor_fill_and_no_1m_section_roi_crop():
    cfg = cloud_configuration()
    cfg["judgment"]["point_cloud"]["spatial_radius_points"] = 0
    obs = [
        point_observation(cfg, i, ms, [10000, 10000]) for i, ms in enumerate([800, 900, 1000], 1)
    ]
    r = calculate(cfg, obs)
    assert r["valid_point_count"] == 2  # Original section is only250mm wide.
    empty = calculate(cfg, [], sensor_index=1)
    assert empty["sensor_id"] == "lidar-b" and empty["xyz_mm"] == []
    assert empty["state"] == "UNAVAILABLE" and empty["anchor"] is None


def test_calibration_mismatch_and_unverified_pose_never_publish_coordinates():
    cfg = cloud_configuration()
    obs = [point_observation(cfg, i, ms, [1000]) for i, ms in enumerate([800, 900, 1000], 1)]
    obs[-1]["calibration_version"] = "other-pose"
    assert calculate(cfg, obs)["valid_point_count"] == 0
    cfg["sensors"][0]["calibration"]["verification_state"] = "UNVERIFIED"
    assert calculate(cfg, obs)["xyz_mm"] == [None]


def test_unverified_filter_model_does_not_publish_coordinates():
    cfg = cloud_configuration()
    obs = [point_observation(cfg, i, ms, [1000]) for i, ms in enumerate([800, 900, 1000], 1)]
    cfg["judgment"]["verification_state"] = "UNVERIFIED"
    result = calculate(cfg, obs)
    assert result["xyz_mm"] == [None]
    assert result["invalid_reason_codes"] == [["FILTER_UNVERIFIED"]]


def test_duplicate_scan_cannot_satisfy_temporal_minimum():
    cfg = cloud_configuration()
    obs = point_observation(cfg, 1, 1000, [1000])
    r = calculate(cfg, [obs, deepcopy(obs), deepcopy(obs)])
    assert r["sample_counts"] == [1] and r["valid"] == [False]
    assert len(r["source_scans"]) == 1


def test_equal_angle_candidates_choose_original_lowest_index():
    cfg = cloud_configuration()
    cfg["judgment"]["point_cloud"]["spatial_radius_points"] = 0
    obs = [
        point_observation(cfg, i, ms, [1000, 9000], angles=[0, 0])
        for i, ms in enumerate([800, 900], 1)
    ]
    obs.append(point_observation(cfg, 3, 1000, [1000], angles=[50]))
    r = calculate(cfg, obs)
    assert r["valid"] == [True]
    assert r["xyz_mm"][0][0] == pytest.approx(1010, abs=0.001)


def test_selected_indices_and_angle_sign_offset_are_preserved():
    cfg = cloud_configuration()
    cfg["judgment"]["point_cloud"]["spatial_radius_points"] = 0
    cfg["sensors"][0]["calibration"].update(angle_sign=-1, angle_zero_offset_mdeg=90000)
    obs = [
        point_observation(cfg, i, ms, [1000] * 4, angles=[359999, 0, 100, 180000])
        for i, ms in enumerate([800, 900, 1000], 1)
    ]
    r = calculate(cfg, obs)
    assert r["point_indices"] == [1, 2]
    assert r["angles_mdeg"] == [0, 100]
    assert r["xyz_mm"][0] == [10, 20, 1030]
