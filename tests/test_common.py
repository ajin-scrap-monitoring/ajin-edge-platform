import json
from datetime import UTC, datetime, timedelta

import pytest
from ajin_edge.clock import classify_clock, probe_chrony, read_clock
from ajin_edge.config import load_config
from ajin_edge.contracts import validate_measurement
from ajin_edge.status import StatusWriter, read_status


def test_invalid_measurement_cannot_carry_stale_fill(measurement):
    measurement["quality"]["state"] = "INVALID"
    with pytest.raises(ValueError):
        validate_measurement(measurement)
    del measurement["fill_ratio"], measurement["fill_percent"]
    validate_measurement(measurement)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1.1])
def test_nonfinite_or_out_of_range_fill_is_rejected(measurement, value):
    measurement["fill_ratio"] = value
    with pytest.raises(ValueError):
        validate_measurement(measurement)


def test_percent_and_ratio_cannot_disagree(measurement):
    measurement["fill_percent"] = 49
    with pytest.raises(ValueError):
        validate_measurement(measurement)


def test_status_is_atomic_and_old_or_wrong_identity_becomes_unknown(tmp_path):
    writer = StatusWriter(tmp_path, "lidar-driver-a", "edge-1", "rev-1")
    writer.write("HEALTHY", scans=5)
    path = tmp_path / "lidar-driver-a.json"
    now = datetime.now(UTC)
    assert read_status(path, now=now)["state"] == "HEALTHY"
    assert read_status(path, now=now + timedelta(seconds=31))["state"] == "UNKNOWN"
    assert read_status(path, now=now - timedelta(seconds=2))["state"] == "UNKNOWN"
    data = json.loads(path.read_text())
    data["service"] = "another-service"
    path.write_text(json.dumps(data))
    assert read_status(path)["state"] == "UNKNOWN"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "offset,state",
    [
        (0, "SYNCED"),
        (-100, "SYNCED"),
        (100.01, "DEGRADED"),
        (500, "DEGRADED"),
        (500.1, "UNSYNCED"),
        (None, "UNSYNCED"),
        (float("nan"), "UNSYNCED"),
    ],
)
def test_clock_offset_boundaries(offset, state):
    assert classify_clock(offset)["state"] == state


def test_missing_or_stale_clock_is_never_assumed_synced(tmp_path):
    path = tmp_path / "clock.json"
    assert read_clock(path)["state"] == "UNSYNCED"
    path.write_text(json.dumps({"offset_ms": 0, "reported_at": "2020-01-01T00:00:00Z"}))
    assert read_clock(path)["state"] == "UNSYNCED"


def test_chrony_csv_includes_reference_name_before_stratum(monkeypatch):
    from types import SimpleNamespace

    # chrony 4.6 client.c process_cmd_tracking prints ref-id AND name (14 columns).
    monkeypatch.setattr(
        "ajin_edge.clock.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            stdout="CB00710F,203.0.113.15,3,1700000000,0.025,0.001,0.002,1,0,0.1,0.01,0.001,64,Normal\n"
        ),
    )
    assert probe_chrony() == {"state": "SYNCED", "offset_ms": 25}


def test_config_rejects_duplicate_sensor_ids_and_path_injection(tmp_path):
    config = {
        "schema_version": "1.0",
        "site_id": "site-1",
        "edge_id": "edge-1",
        "config_revision": "rev-1",
        "sensors": [{"sensor_id": "lidar-a"}, {"sensor_id": "lidar-a"}],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_config(path)
    config["sensors"][1]["sensor_id"] = "../lidar-b"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("bad_reasons", [None, "SENSOR_ABSENT", [42]])
def test_corrupt_status_reason_list_cannot_crash_orchestrator(tmp_path, bad_reasons):
    writer = StatusWriter(tmp_path, "lidar-processing", "edge-1", "rev-1")
    writer.write("DEGRADED")
    value = json.loads(writer.path.read_text())
    value["reason_codes"] = bad_reasons
    writer.path.write_text(json.dumps(value))
    assert read_status(writer.path)["state"] == "UNKNOWN"
