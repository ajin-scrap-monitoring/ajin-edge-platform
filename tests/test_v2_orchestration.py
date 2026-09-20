import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.orchestration import ConfigurationGate, Orchestrator
from ajin_edge.v2.processing import envelope, transform_scan
from test_v2_processing import configuration, scan


def test_no_invented_fill_or_event():
    orchestrator = Orchestrator(configuration())
    orchestrator.ingest(transform_scan(scan(), configuration()["sensors"][0]), now_ns=950000000)
    state = orchestrator.assessment()
    validate_message(state)
    assert state["fill_estimate"] is None
    assert state["fill_state"] == "UNAVAILABLE"
    assert state["capabilities"]["fill"] == "NOT_IMPLEMENTED"
    assert state["evidence"]["scans"] == []
    assert state["observed_from"] is None
    with pytest.raises(ValueError):
        orchestrator.event("OCCLUSION", "CONFIRMED", [], rule_version=None)


def test_stall_recovery_needs_continuous_new_scans_for_one_second():
    config = configuration()
    orch = Orchestrator(config)

    def ingest(sequence, ns):
        orch.ingest(
            transform_scan(scan(sequence=sequence, ms=ns // 1000000), config["sensors"][0]),
            now_ns=ns,
        )

    ingest(1, 0)
    assert orch.sensor_states(now_ns=0)[0]["data_state"] == "FRESH"
    assert orch.sensor_states(now_ns=1000000001)[0]["data_state"] == "STALE"
    ingest(2, 1100000000)
    assert orch.sensor_states(now_ns=1100000000)[0]["data_state"] == "RECOVERING"
    ingest(3, 1600000000)
    assert orch.sensor_states(now_ns=1600000000)[0]["data_state"] == "RECOVERING"
    ingest(4, 2100000000)
    assert orch.sensor_states(now_ns=2100000000)[0]["data_state"] == "FRESH"


def test_replayed_scan_never_refreshes_freshness():
    config = configuration()
    orch = Orchestrator(config)
    observation = transform_scan(scan(ms=0), config["sensors"][0])
    orch.ingest(observation, now_ns=0)
    orch.ingest(observation, now_ns=2000000000)
    assert orch.sensor_states(now_ns=2000000000)[0]["data_state"] == "STALE"


def test_configuration_only_activates_matching_remote_ack_and_revision_is_immutable():
    config = configuration()
    request = {**config, **envelope(config, "edge_configuration")}
    gate = ConfigurationGate()
    gate.propose(request)
    assert gate.active is None
    assert gate.acknowledge("unrelated") is False
    assert gate.acknowledge(request["message_id"]) is True
    assert gate.active["config_revision"] == "cfg-2"
    changed = {**request, "message_id": "other", "edge_id": "changed"}
    with pytest.raises(ValueError):
        gate.propose(changed)
    gate.invalidate_installation()
    assert all(
        s["calibration"]["verification_state"] == "UNVERIFIED" for s in gate.effective()["sensors"]
    )
