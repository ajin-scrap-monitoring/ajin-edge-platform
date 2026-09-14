import asyncio
import json
from types import SimpleNamespace

import pytest
from test_processing import config


@pytest.mark.asyncio
async def test_runtime_absent_sensors_reports_retrying_and_cancels(tmp_path):
    from ajin_lidar_processing.runtime import run

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config()))
    args = SimpleNamespace(
        config=cfg, status_dir=tmp_path, clock_file=tmp_path / "clock.json", uplink="localhost:1"
    )
    task = asyncio.create_task(run(args))
    try:
        for _ in range(30):
            await asyncio.sleep(0.05)
            path = tmp_path / "lidar-processing.json"
            if path.exists():
                break
        state = json.loads(path.read_text())
        assert state["service"] == "lidar-processing"
        assert state["state"] == "RETRYING"
        assert state["last_measurement_id"]
        assert "SENSOR_ABSENT" in state["reason_codes"]
        assert "CLOCK_UNSYNCED" in state["reason_codes"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_calibration_failure_replaces_previous_healthy_status(tmp_path):
    from ajin_lidar_processing.runtime import run

    cfg = tmp_path / "config.json"
    c = config()
    c["sensors"][0]["calibration"]["rotation"][0][0] = 2
    cfg.write_text(json.dumps(c))
    path = tmp_path / "lidar-processing.json"
    path.write_text(json.dumps({"state": "HEALTHY", "instance_id": "old"}))
    args = SimpleNamespace(
        config=cfg, status_dir=tmp_path, clock_file=tmp_path / "clock.json", uplink="localhost:1"
    )
    with pytest.raises(ValueError):
        await run(args)
    state = json.loads(path.read_text())
    assert state["state"] == "FATAL"
    assert state["reason_codes"] == ["CALIBRATION_INVALID"]
    assert state["instance_id"] != "old"
