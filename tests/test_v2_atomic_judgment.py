from ajin_edge.v2.orchestration import Orchestrator
from test_v2_judgment import judgment_configuration, observation


def captured_input():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    for seq, ms in enumerate([600, 700, 800], 1):
        for sid in ["lidar-a", "lidar-b"]:
            orch.ingest(observation(cfg, sid, seq, ms), now_ns=ms * 1000000)
    return cfg, orch, orch.judgment.snapshot(800000000)


def test_matching_check_and_calculation_use_the_same_snapshot():
    cfg, orch, captured = captured_input()
    assert orch.judgment._anchors(captured) is not None
    # A advances after the readiness check but before the worker starts.
    orch.ingest(observation(cfg, "lidar-a", 4, 901), now_ns=901000000)
    assert "SENSOR_TIME_MISMATCH" in orch.assessment(now_ns=901000000)["reason_codes"]
    body = orch.assessment_bundle(now_ns=901000000, histories=captured)[-1]
    assert "SENSOR_TIME_MISMATCH" not in body["reason_codes"]
    assert body["fusion_result"]["pair_delta_ms"] == 0
    assert {c["anchor_scan_id"] for c in body["fusion_result"]["components"]} == {
        h[-1]["scan"]["scan_id"] for h in captured.values()
    }
    assert orch.judgment.history["lidar-a"][-1]["scan"]["sequence"] == 4


def test_captured_input_is_rechecked_for_expiry_before_calculation():
    _, orch, captured = captured_input()
    body = orch.assessment_bundle(now_ns=1801000000, histories=captured)[-1]
    assert body["fusion_result"] is None
    assert body["fill_state"] == "UNAVAILABLE"
    assert not body["evidence"]["scans"]


def test_captured_mismatch_is_not_replaced_by_an_older_matching_pair():
    cfg, orch, _ = captured_input()
    orch.ingest(observation(cfg, "lidar-a", 4, 901), now_ns=901000000)
    captured = orch.judgment.snapshot(901000000)
    body = orch.assessment_bundle(now_ns=901000000, histories=captured)[-1]
    assert "SENSOR_TIME_MISMATCH" in body["reason_codes"]
    assert body["fusion_result"] is None
