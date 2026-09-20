from copy import deepcopy

import pytest
from ajin_edge.v2.processing import ProcessingEngine, transform_scan, validate_configuration


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
        "acquired_at": "2026-09-20T00:00:00.950Z",
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
    message = engine.periodic(now_ns=1000000000)
    assert message["pair_state"] == "MATCHED"
    assert message["pair_delta_ms"] == 10
    assert [s["scan"]["sequence"] for s in message["sensors"]] == [1, 1]
    following = engine.periodic(now_ns=1100000000)
    assert following["pair_state"] == "PARTIAL"
    assert following["sensors"][0]["scan"]["sequence"] == 2
    assert following["sensors"][1]["scan"] is None


@pytest.mark.parametrize("delta,state", [(100, "MATCHED"), (101, "UNMATCHED")])
def test_pair_boundary(delta, state):
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=1000))
    engine.ingest(scan("lidar-b", ms=1000 - delta))
    assert engine.periodic(now_ns=1000000000)["pair_state"] == state


def test_freshness_boundary_and_no_new_scan():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan(ms=0))
    engine.ingest(scan("lidar-b", ms=0))
    assert engine.periodic(now_ns=1000000000)["pair_state"] == "MATCHED"
    assert engine.periodic(now_ns=1000000001)["pair_state"] == "NONE"
    engine.ingest(scan(sequence=2, ms=0))
    assert engine.periodic(now_ns=1000000001)["sensors"][0]["availability"] == "STALE"


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
        engine.periodic(now_ns=1000000000)["sensors"][0]["scan"]["points"][0]["distance_mm"] == 1000
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
    sensor = engine.periodic(now_ns=1000000000)["sensors"][0]
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
