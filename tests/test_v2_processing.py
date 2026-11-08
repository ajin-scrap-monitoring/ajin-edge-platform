import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from ajin_edge.v2.processing import (
    ProcessingEngine,
    round_mm,
    transform_scan,
    validate_configuration,
)


def configuration():
    return {
        "site_id": "site-test",
        "edge_id": "edge-test",
        "config_revision": "cfg-2",
        "coordinate_frame": {"frame_id": "frame-test"},
        "sensors": [
            {
                "sensor_id": sensor,
                "angle_intervals_mdeg": [{"start_inclusive": 0, "end_exclusive": 180000}],
                "calibration": {
                    "version": "cal-test",
                    "verification_state": "VERIFIED",
                    "angle_sign": 1,
                    "angle_zero_offset_mdeg": 0,
                    "rotation": [[1, 0, 0], [0, 0, -1], [0, 1, 0]],
                    "translation_mm": [0, 0, 3000],
                },
            }
            for sensor in ("lidar-a", "lidar-b")
        ],
    }


def scan(sensor="lidar-a", sequence=1, ms=950, domain="boot-test"):
    return {
        "sensor_id": sensor,
        "scan_id": f"{sensor}-{sequence}",
        "stream_instance_id": "stream-test",
        "sequence": sequence,
        "acquired_at": (datetime(2026, 9, 20, tzinfo=UTC) + timedelta(milliseconds=ms)).isoformat(),
        "timestamp_source": "EDGE_SCAN_RECEIVED",
        "clock_domain_id": domain,
        "acquired_monotonic_ns": str(ms * 1000000),
        "clock_state": "UNSYNCED",
        "clock_offset_ms": None,
        "samples": [
            {"angle_mdeg": 90000, "distance_mm": 1000, "quality": 0},
            {"angle_mdeg": 180000, "distance_mm": 2222, "quality": 63},
        ],
    }


def test_raw_low_quality_is_preserved_and_only_angles_select():
    cfg = configuration()
    raw = scan()
    original = deepcopy(raw)
    result = transform_scan(raw, cfg["sensors"][0])
    assert raw == original
    assert result["scan"]["source_point_count"] == 2
    assert result["scan"]["selected_point_count"] == 1
    assert result["scan"]["points"] == [
        {
            "index": 0,
            "angle_mdeg": 90000,
            "distance_mm": 1000,
            "quality": 0,
            "position_mm": {"x": 0, "y": 0, "z": 4000},
            "calculation_reason_codes": [],
        }
    ]


@pytest.mark.parametrize(
    "calibration,reason", [(None, "CALIBRATION_MISSING"), ("unverified", "CALIBRATION_UNVERIFIED")]
)
def test_unverified_transform_never_invents_position(calibration, reason):
    sensor = configuration()["sensors"][0]
    if calibration is None:
        sensor["calibration"] = None
    else:
        sensor["calibration"]["verification_state"] = "UNVERIFIED"
    result = transform_scan(scan(), sensor)
    assert result["transform_reason_codes"] == [reason]
    assert result["scan"]["points"][0]["position_mm"] is None
    assert result["scan"]["points"][0]["calculation_reason_codes"] == []


def test_rounding_is_half_away_from_zero_and_float_calculation_retained():
    sensor = configuration()["sensors"][0]
    sensor["calibration"]["translation_mm"] = [-0.5, 0.5, 0]
    raw = scan()
    raw["samples"] = [{"angle_mdeg": 0, "distance_mm": 2, "quality": 63}]
    result = transform_scan(raw, sensor)
    assert result["scan"]["points"][0]["position_mm"] == {"x": 2, "y": 1, "z": 0}


def test_full_scan_projection_matches_reference_rotation_without_changing_original_points():
    sensor = configuration()["sensors"][0]
    sensor["angle_intervals_mdeg"] = [{"start_inclusive": 0, "end_exclusive": 360000}]
    a, b = 0.73, -0.32
    rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    cal = sensor["calibration"]
    cal.update(
        rotation=(rz @ ry).tolist(),
        translation_mm=[-0.5, 1000.25, -2000.5],
        angle_sign=-1,
        angle_zero_offset_mdeg=1234,
    )
    raw = scan()
    raw["samples"] = [
        {
            "angle_mdeg": i * 351 % 360000,
            "distance_mm": i * 103 % 40000,
            "quality": i % 256,
            "sdk_invalid_range": i % 101 == 0,
        }
        for i in range(1025)
    ]
    original = deepcopy(raw)
    result = transform_scan(raw, sensor)
    for point in result["scan"]["points"]:
        sample = raw["samples"][point["index"]]
        assert all(point[k] == sample[k] for k in ("angle_mdeg", "distance_mm", "quality"))
        if sample["sdk_invalid_range"]:
            assert point["position_mm"] is None
            continue
        theta = math.radians((-sample["angle_mdeg"] + 1234) / 1000)
        local = np.array(
            [sample["distance_mm"] * math.cos(theta), sample["distance_mm"] * math.sin(theta), 0]
        )
        expected = np.asarray(cal["rotation"]) @ local + cal["translation_mm"]
        assert point["position_mm"] == dict(
            zip(("x", "y", "z"), map(round_mm, expected), strict=True)
        )
    assert raw == original


def test_observation_snapshot_owns_every_mutable_wire_container():
    from ajin_edge.v2 import processing

    assert hasattr(processing, "copy_observation"), "observation snapshot copier is missing"
    original = transform_scan(scan(), configuration()["sensors"][0])
    before = deepcopy(original)
    copied = processing.copy_observation(original)
    assert copied == original
    copied["angle_intervals_mdeg"][0]["end_exclusive"] = 100
    copied["transform_reason_codes"].append("TEST")
    copied["scan"]["points"][0]["position_mm"]["z"] = 0
    copied["scan"]["points"][0]["calculation_reason_codes"].append("TEST")
    copied["scan"]["points"].clear()
    assert original == before


def test_configuration_rejects_overlap_and_non_rotation():
    cfg = configuration()
    cfg["sensors"][0]["angle_intervals_mdeg"].append(
        {"start_inclusive": 170000, "end_exclusive": 190000}
    )
    with pytest.raises(ValueError):
        validate_configuration(cfg)
    cfg = configuration()
    cfg["sensors"][0]["calibration"]["rotation"][0][0] = 2
    with pytest.raises(ValueError):
        validate_configuration(cfg)


def test_latest_pair_uses_older_matching_scan_not_arbitrary_latest():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    for raw in [scan(ms=800), scan(sequence=2, ms=990), scan("lidar-b", ms=810)]:
        engine.ingest(raw)
    message = engine.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01.000Z")
    assert message["pair_state"] == "MATCHED"
    assert message["pair_delta_ms"] == 10
    assert [s["scan"]["sequence"] for s in message["sensors"]] == [1, 1]
    following = engine.periodic(now_ns=1100000000, generated_at="2026-09-20T00:00:01.000Z")
    assert following["pair_state"] == "PARTIAL"
    assert following["sensors"][0]["scan"]["sequence"] == 2
    assert following["sensors"][1]["scan"] is None


@pytest.mark.parametrize("delta,state", [(100, "MATCHED"), (101, "UNMATCHED")])
def test_pair_boundary(delta, state):
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=1000))
    engine.ingest(scan("lidar-b", ms=1000 - delta))
    assert (
        engine.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01.000Z")["pair_state"]
        == state
    )


def test_freshness_boundary_and_no_new_scan():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=0))
    engine.ingest(scan("lidar-b", ms=0))
    assert (
        engine.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01.000Z")["pair_state"]
        == "MATCHED"
    )
    assert (
        engine.periodic(now_ns=1000000001, generated_at="2026-09-20T00:00:01.000Z")["pair_state"]
        == "NONE"
    )
    engine.ingest(scan(sequence=2, ms=0))
    assert (
        engine.periodic(now_ns=1000000001, generated_at="2026-09-20T00:00:01.000Z")["sensors"][0][
            "availability"
        ]
        == "STALE"
    )


def test_wrong_clock_and_conflicting_scan_are_rejected_without_overwrite():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    with pytest.raises(ValueError):
        engine.ingest(scan(domain="other-boot"))
    assert engine.ingest(scan()) is not None
    assert engine.ingest(scan()) is None
    modified = scan()
    modified["samples"][0]["distance_mm"] = 99
    with pytest.raises(ValueError):
        engine.ingest(modified)
    assert (
        engine.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01.000Z")["sensors"][0][
            "scan"
        ]["points"][0]["distance_mm"]
        == 1000
    )


def test_all_ingested_scans_available_and_buffer_loss_counted():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test", capacity=2)
    observed = [engine.ingest(scan(sequence=i, ms=i)) for i in range(1, 5)]
    assert [x["scan"]["sequence"] for x in observed] == [1, 2, 3, 4]
    assert engine.dropped_scans == 2


def test_unconfigured_angles_are_not_defaulted():
    cfg = configuration()
    cfg["sensors"][0]["angle_intervals_mdeg"] = []
    engine = ProcessingEngine(cfg, clock_domain_id="boot-test")
    engine.ingest(scan())
    sensor = engine.periodic(now_ns=1000000000, generated_at="2026-09-20T00:00:01.000Z")["sensors"][
        0
    ]
    assert sensor["availability"] == "CONFIG_UNAVAILABLE"
    assert sensor["scan"] is None


def test_hq_quality_byte_and_sdk_invalid_range_are_preserved_without_filtering():
    raw = scan()
    raw["samples"] = [
        {"angle_mdeg": 1000, "distance_mm": 0, "quality": 252, "sdk_invalid_range": True},
        {"angle_mdeg": 1000, "distance_mm": 0, "quality": 1},
    ]
    result = transform_scan(raw, configuration()["sensors"][0])
    points = result["scan"]["points"]
    assert len(points) == 2
    assert points[0]["quality"] == 252
    assert points[0]["position_mm"] is None
    assert points[0]["calculation_reason_codes"] == ["SDK_INVALID_RANGE"]
    assert "sdk_invalid_range" not in points[0]
    # q2 below one mm rounds down; only the SDK flag, not rounded mm, is invalid.
    assert points[1]["position_mm"] is not None


@pytest.mark.parametrize(
    "age_ms,expected", [(999, "PRESENT"), (1000, "PRESENT"), (1001, "STALE"), (1010, "STALE")]
)
def test_periodic_checks_age_at_generation_not_only_selection(age_ms, expected):
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=0))
    engine.ingest(scan("lidar-b", ms=0))
    generated = (datetime(2026, 9, 20, tzinfo=UTC) + timedelta(milliseconds=age_ms)).isoformat()
    body = engine.periodic(now_ns=1_000_000_000, generated_at=generated)
    assert [s["availability"] for s in body["sensors"]] == [expected, expected]
    assert body["pair_state"] == ("MATCHED" if expected == "PRESENT" else "NONE")
    assert body["pair_delta_ms"] == (0 if expected == "PRESENT" else None)


def test_generation_delay_reselects_newer_single_scan_and_preserves_its_identity(monkeypatch):
    import ajin_edge.v2.processing as module

    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    for raw in (scan(ms=0), scan("lidar-b", ms=0), scan(sequence=2, ms=200)):
        engine.ingest(raw)
    monkeypatch.setattr(module, "utc_now", lambda: "2026-09-20T00:00:01.010Z")
    body = engine.periodic(now_ns=1_000_000_000)
    assert body["generated_at"] == "2026-09-20T00:00:01.010Z"
    assert body["pair_state"] == "PARTIAL"
    assert body["pair_delta_ms"] is None
    assert body["sensors"][0]["scan"]["scan_id"] == "lidar-a-2"
    assert body["sensors"][1]["availability"] == "STALE"
    assert body["sensors"][1]["scan"] is None


def test_future_utc_is_rejected_even_with_fresh_monotonic_age():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=950))
    body = engine.periodic(now_ns=1_000_000_000, generated_at="2026-09-20T00:00:00.900Z")
    assert body["pair_state"] == "NONE"
    assert body["sensors"][0]["scan"] is None


def test_periodic_rechecks_utc_after_payload_preparation(monkeypatch):
    import ajin_edge.v2.processing as module

    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=0))
    prepared = [False]
    original = module.deepcopy

    def copy_then_delay(value):
        result = original(value)
        if isinstance(value, dict) and value.get("availability") == "PRESENT":
            prepared[0] = True
        return result

    monkeypatch.setattr(module, "deepcopy", copy_then_delay)
    monkeypatch.setattr(
        module,
        "utc_now",
        lambda: "2026-09-20T00:00:01.010Z" if prepared[0] else "2026-09-20T00:00:01.000Z",
    )
    body = engine.periodic(now_ns=1_000_000_000)
    assert body["sensors"][0]["availability"] == "STALE"
