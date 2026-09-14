import json
from pathlib import Path

import pytest
from ajin_lidar_processing.replay import replay_records

ROOT = Path(__file__).resolve().parents[1]


def test_replay_is_deterministic_and_disconnected_scans_never_repeat_fill():
    config = json.loads(
        (ROOT / "deploy/config.example/processing.example.json").read_text()
    )
    config["allow_demo_calibration"] = True
    records = [
        json.loads(line)
        for line in (ROOT / "integration-tests/replay-data/half-full-then-loss.jsonl")
        .read_text()
        .splitlines()
    ]
    first = list(replay_records(records, config))
    assert first == list(replay_records(records, config))
    assert len(first) == 2
    assert first[0]["quality"]["state"] == "GOOD"
    assert first[0]["fill_percent"] == pytest.approx(50, abs=1)
    assert first[1]["quality"]["state"] == "INVALID"
    assert "fill_percent" not in first[1]


def test_replay_refuses_demo_without_explicit_opt_in():
    config = json.loads(
        (ROOT / "deploy/config.example/processing.example.json").read_text()
    )
    assert config["allow_demo_calibration"] is False
    with pytest.raises(ValueError):
        list(replay_records([], config))
