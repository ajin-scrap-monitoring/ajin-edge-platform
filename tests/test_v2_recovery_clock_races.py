import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ajin_edge.v2 import runtime
from ajin_edge.v2.orchestration import Orchestrator
from test_v2_processing import configuration


async def test_background_judgment_does_not_mutate_recovery_observation_clock():
    orch = Orchestrator(configuration())
    bundle = await asyncio.to_thread(orch.assessment_bundle)
    assert bundle[-1]["message_type"] == "edge_assessment"
    assert orch.recovery_evidence is None


@pytest.mark.parametrize("offset_ms,expected", [(5, True), (20, False)])
def test_ack_clock_is_sampled_after_atomic_status_read(tmp_path, monkeypatch, offset_ms, expected):
    base = datetime(2026, 11, 11, tzinfo=UTC)
    clock = [base]
    stamp = (base + timedelta(milliseconds=offset_ms)).isoformat()
    registration = {"site_id": "s", "edge_id": "e", "config_revision": "r"}
    config = {
        "status_dir": str(tmp_path),
        "configuration": {
            **registration,
            "sensors": [{"sensor_id": "lidar-a"}],
        },
    }
    source = {
        **registration,
        "reported_at": stamp,
        "state": "HEALTHY",
        "http_delivery": {
            k: {"latest_accepted_generated_at": stamp, "latest_accepted_observed_at": stamp}
            for k in ["PERIODIC", "STATE"]
        },
    }
    source["http_delivery"]["PERIODIC"]["latest_accepted_sensors"] = {
        "lidar-a": {"generated_at": stamp, "observed_at": stamp, "acquired_at": stamp}
    }
    (tmp_path / "measurement-uplink.json").write_text(json.dumps(source))
    original_read = Path.read_bytes

    def read_completed(path):
        result = original_read(path)
        clock[0] = base + timedelta(milliseconds=10)
        return result

    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz):
            return clock[0]

    monkeypatch.setattr(Path, "read_bytes", read_completed)
    monkeypatch.setattr(runtime, "datetime", Clock)
    assert runtime.recovery_delivery_healthy(config) is expected
