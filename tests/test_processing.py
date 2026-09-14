import math
from types import SimpleNamespace as NS

import pytest


def config():
    return dict(
        schema_version="1.0",
        site_id="site",
        edge_id="edge",
        config_revision="r1",
        camera_id="cam",
        calibration=dict(
            version="real-v1", demo=False, fusion_map=[[0, 0], [1, 1]], single_sensor_maps={}
        ),
        processing=dict(target_scan_hz=10),
        sensors=[
            dict(
                sensor_id=s,
                endpoint="unix:/tmp/" + s,
                base_weight=0.5,
                calibration=dict(
                    rotation=[[1, 0, 0], [0, 0, 1], [0, -1, 0]],
                    translation_mm=[0, 0, 0],
                    roi_x_mm=[0, 200],
                    roi_z_mm=[0, 1000],
                    masks_x_mm=[],
                    bottom_mm=[0] * 4,
                    max_height_mm=[100] * 4,
                ),
            )
            for s in ["a", "b"]
        ],
    )


def frame(sensor="a", seq=1, stamp=1_000_000_000, height=50, instance="i", revision="r1"):
    samples = []
    for x in [25, 75, 125, 175]:
        samples.append(
            NS(
                angle_mdeg=round(math.degrees(math.atan2(height, x)) * 1000),
                distance_mm=round(math.hypot(x, height)),
                quality=40,
            )
        )
    return NS(
        schema_version="1.0",
        edge_id="edge",
        sensor_id=sensor,
        sequence=seq,
        instance_id=instance,
        config_revision=revision,
        sdk_status="OK",
        scan_hz=10,
        acquired_monotonic_ns=stamp,
        acquired_at_unix_ms=100000 + stamp // 1_000_000,
        samples=samples,
    )


def engine():
    from ajin_lidar_processing.engine import ProcessingEngine

    return ProcessingEngine(config())


def test_hand_derived_half_full_and_no_reuse():
    e = engine()
    e.ingest(frame())
    e.ingest(frame("b"))
    m = e.measure(1_000_000_000, 101000, dict(state="SYNCED", offset_ms=10))
    assert m["fill_ratio"] == pytest.approx(0.5, abs=0.01)
    assert m["quality"]["state"] == "GOOD"
    assert m["camera_reference"]["window_start"] == "1970-01-01T00:01:39.000Z"
    assert m["camera_reference"]["window_end"] == "1970-01-01T00:01:43.000Z"
    from ajin_edge.contracts import validate_measurement

    validate_measurement(m)
    assert e.measure(1_500_000_000, 101500, dict(state="SYNCED", offset_ms=0)) is None


def test_absent_sensor_and_stale_omit_fill():
    e = engine()
    e.ingest(frame())
    m = e.measure(1_600_000_000, 101600, dict(state="UNSYNCED", offset_ms=None))
    assert m["quality"]["state"] == "INVALID" and "fill_ratio" not in m
    assert "camera_reference" not in m
    m = e.measure(4_000_000_000, 104000, dict(state="SYNCED", offset_ms=0))
    assert m["quality"]["state"] == "INVALID" and "fill_ratio" not in m


def test_revision_block_restart_and_bounded_buffer():
    e = engine()
    assert not e.ingest(frame(revision="wrong"))
    assert e.measure(1_000_000_000, 101000, {}) is None
    for n in range(1, 25):
        assert e.ingest(frame(seq=n, stamp=n * 100_000_000))
    assert len(e.buffers["a"]) == 10
    assert not e.ingest(frame(seq=24, stamp=2_400_000_000))
    assert e.ingest(frame(seq=1, stamp=2_500_000_000, instance="restart"))


@pytest.mark.parametrize("mutation", ["matrix", "map", "weight", "demo"])
def test_bad_calibration_rejected(mutation):
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    if mutation == "matrix":
        c["sensors"][0]["calibration"]["rotation"][0][0] = 2
    if mutation == "map":
        c["calibration"]["fusion_map"] = [[0, 1], [1, 0]]
    if mutation == "weight":
        c["sensors"][0]["base_weight"] = -1
    if mutation == "demo":
        c["calibration"]["demo"] = True
    with pytest.raises(ValueError):
        ProcessingEngine(c)


def test_future_and_invalid_samples_cannot_publish_good():
    e = engine()
    f = frame()
    f.samples = []
    assert not e.ingest(f)
    e.ingest(frame(stamp=10_000_000_000))
    e.ingest(frame("b", stamp=10_000_000_000))
    m = e.measure(1_000_000_000, 101000, {})
    assert m["quality"]["state"] == "INVALID"


def test_outlier_then_sustained_surface_movement():
    e = engine()
    for seq, h in enumerate([50, 50, 50, 50, 50, 90, 50, 90, 90, 90], 1):
        for s in ["a", "b"]:
            e.ingest(frame(s, seq, seq * 100_000_000, h))
        m = e.measure(seq * 100_000_000, 100000 + seq * 100, dict(state="DEGRADED", offset_ms=200))
        if seq == 6:
            assert m["fill_ratio"] == pytest.approx(0.5, abs=0.01)
    assert m["fill_ratio"] == pytest.approx(0.9, abs=0.01)
    assert "CLOCK_DEGRADED" in m["quality"]["reason_codes"]


def test_polygon_and_masks_exclude_ineligible_section():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    for s in c["sensors"]:
        s["calibration"]["roi_polygon_xz_mm"] = [[0, 0], [150, 0], [150, 100], [0, 100]]
        s["calibration"]["masks_x_mm"] = [[100, 200]]
    e = ProcessingEngine(c)
    for s in ["a", "b"]:
        e.ingest(frame(s))
    m = e.measure(1_000_000_000, 101000, {})
    assert m["sensors"][0]["coverage_ratio"] == 1
    assert m["fill_ratio"] == pytest.approx(0.5, abs=0.01)


def test_pending_queue_retains_retry_id_and_bounds():
    from ajin_lidar_processing.runtime import PendingQueue

    q = PendingQueue(2)
    q.put({"measurement_id": "a"})
    q.put({"measurement_id": "b"})
    q.put({"measurement_id": "c"})
    assert q.loss == 1 and q.peek()["measurement_id"] == "b"
    assert q.peek()["measurement_id"] == "b"
    q.ack("b")
    assert q.peek()["measurement_id"] == "c"


def test_polygon_rejects_points_outside_irregular_shape():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    for s in c["sensors"]:
        s["calibration"]["roi_polygon_xz_mm"] = [[0, 0], [200, 0], [0, 20]]
    e = ProcessingEngine(c)
    for s in ["a", "b"]:
        e.ingest(frame(s))
    assert e.measure(1_000_000_000, 101000, {})["quality"]["state"] == "INVALID"


def test_calibrated_single_sensor_fallback_and_disagreement():
    from ajin_edge.contracts import validate_measurement
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    c["calibration"]["single_sensor_maps"] = {"a": [[0, 0], [1, 0.8]]}
    e = ProcessingEngine(c)
    e.ingest(frame())
    m = e.measure(1_600_000_000, 101600, {})
    assert m["quality"]["state"] == "DEGRADED"
    assert m["fill_ratio"] == pytest.approx(0.4, abs=0.01)
    validate_measurement(m)
    e = engine()
    e.ingest(frame(height=10))
    e.ingest(frame("b", height=90))
    m = e.measure(1_000_000_000, 101000, {})
    assert m["quality"]["state"] == "DEGRADED"
    assert m["quality"]["confidence"] == pytest.approx(0.7)
    assert "SENSOR_DISAGREEMENT" in m["quality"]["reason_codes"]


def test_utc_jump_and_oversize_scan_invalid():
    e = engine()
    f = frame()
    f.samples *= 10000
    assert not e.ingest(f)
    e.ingest(frame())
    e.ingest(frame("b"))
    m = e.measure(1_000_000_000, 110000, {})
    assert m["quality"]["state"] == "INVALID"
    assert "fill_ratio" not in m


def test_continuous_single_sensor_cannot_reset_pair_deadline():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    c["calibration"]["single_sensor_maps"] = {"a": [[0, 0], [1, 1]]}
    e = ProcessingEngine(c)
    measurements = []
    for seq in range(1, 31):
        stamp = seq * 100_000_000
        e.ingest(frame(seq=seq, stamp=stamp))
        if seq % 10 == 0:
            measurements.append(e.measure(stamp, 100000 + seq * 100, {}))
    assert measurements[1] is not None
    assert measurements[1]["quality"]["state"] == "DEGRADED"
    assert measurements[2]["quality"]["state"] == "DEGRADED"


def test_configured_angle_gate_and_invalid_expected_returns():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    for sensor in c["sensors"]:
        sensor["sample_filter"] = dict(
            distance_min_mm=1, distance_max_mm=1000, quality_min=30, angle_interval_mdeg=[0, 90000]
        )
    e = ProcessingEngine(c)
    for sid in ["a", "b"]:
        f = frame(sid)
        f.samples += [NS(angle_mdeg=200000, distance_mm=0, quality=0)] * 100
        f.samples += [NS(angle_mdeg=30000, distance_mm=0, quality=0)] * 4
        e.ingest(f)
    m = e.measure(1_000_000_000, 101000, {})
    assert m["sensors"][0]["valid_sample_ratio"] == 0.5
    assert m["quality"]["state"] == "DEGRADED"


def test_bad_filter_rejected():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    c["sensors"][0]["sample_filter"] = dict(distance_min_mm=100, distance_max_mm=10)
    with pytest.raises(ValueError):
        ProcessingEngine(c)


def test_future_frame_does_not_poison_same_instance_history():
    e = engine()
    e.ingest(frame(seq=900, stamp=86_400_000_000_000))
    e.measure(1_000_000_000, 101000, {})
    assert e.ingest(frame(seq=2, stamp=2_000_000_000))
    e.ingest(frame("b", seq=2, stamp=2_000_000_000))
    assert e.measure(2_000_000_000, 102000, {})["quality"]["state"] == "GOOD"


def test_polygon_full_column_mask_excludes_area_and_coverage():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    for sensor in c["sensors"]:
        sensor["calibration"]["mask_polygons_xz_mm"] = [
            [[50, 0], [100, 0], [100, 1000], [50, 1000]]
        ]
    e = ProcessingEngine(c)
    for sid in ["a", "b"]:
        e.ingest(frame(sid))
    m = e.measure(1_000_000_000, 101000, {})
    assert m["sensors"][0]["coverage_ratio"] == 1
    assert m["fill_ratio"] == pytest.approx(0.5, abs=0.01)


@pytest.mark.parametrize("mask", ["column", "polygon"])
def test_partial_column_mask_rejected(mask):
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    cal = c["sensors"][0]["calibration"]
    if mask == "column":
        cal["masks_x_mm"] = [[60, 100]]
    else:
        cal["mask_polygons_xz_mm"] = [[[50, 0], [100, 0], [100, 500], [50, 500]]]
    with pytest.raises(ValueError, match="full-column"):
        ProcessingEngine(c)


def test_normal_history_eviction_is_not_frame_loss():
    e = engine()
    for seq in range(1, 30):
        for sid in ["a", "b"]:
            e.ingest(frame(sid, seq, seq * 100_000_000))
        e.measure(seq * 100_000_000, 100000 + seq * 100, {})
    assert e.frame_loss == 0


def test_malformed_global_angle_rejects_frame():
    e = engine()
    f = frame()
    f.samples.append(NS(angle_mdeg=400000, distance_mm=100, quality=40))
    assert not e.ingest(f)


def test_receive_clock_rejection_and_pending_replay_recovery():
    e = engine()
    assert not e.ingest(
        frame(seq=900, stamp=86_400_000_000_000),
        receive_monotonic_ns=1_000_000_000,
        receive_unix_ms=101000,
    )
    assert e.ingest(frame(), receive_monotonic_ns=1_000_000_000, receive_unix_ms=101000)
    replay = engine()
    replay.ingest(frame(seq=900, stamp=86_400_000_000_000))
    replay.ingest(frame(seq=2, stamp=2_000_000_000))
    replay.ingest(frame("b", seq=2, stamp=2_000_000_000))
    assert replay.measure(2_000_000_000, 102000, {})["quality"]["state"] == "GOOD"


@pytest.mark.parametrize("receive_clocks", [False, True])
@pytest.mark.parametrize("previous_measurement", [False, True])
def test_reversed_pending_sequences_cannot_replace_latest(receive_clocks, previous_measurement):
    e = engine()
    if previous_measurement:
        for sid in ["a", "b"]:
            e.ingest(frame(sid, seq=1, stamp=1_000_000_000))
        e.measure(1_000_000_000, 101000, {})
    clocks = (
        dict(receive_monotonic_ns=1_300_000_000, receive_unix_ms=101300) if receive_clocks else {}
    )
    for sid in ["a", "b"]:
        e.ingest(frame(sid, seq=3, stamp=1_300_000_000), **clocks)
        e.ingest(frame(sid, seq=2, stamp=1_200_000_000, height=90), **clocks)
    m = e.measure(1_300_000_000, 101300, {})
    assert [sensor["sequence"] for sensor in m["sensors"]] == [3, 3]
    assert m["fill_ratio"] == pytest.approx(0.5, abs=0.01)
    assert all(f.sequence != 2 for buf in e.buffers.values() for f in buf)
    assert e.frame_loss >= 2
