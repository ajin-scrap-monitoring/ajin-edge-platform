from copy import deepcopy

import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.processing import transform_scan
from test_v2_processing import configuration, scan


def ingest(orch, ms, sequence, sensor="lidar-a", received_ms=None):
    cfg = next(s for s in orch.config["sensors"] if s["sensor_id"] == sensor)
    return orch.ingest(
        transform_scan(scan(sensor=sensor, sequence=sequence, ms=ms), cfg),
        now_ns=(ms if received_ms is None else received_ms) * 10**6,
    )


def alarm(orch, ms, sensor="lidar-a"):
    return orch.scan_update_alarms(now_ns=ms * 10**6)[sensor]


@pytest.mark.parametrize(
    "ms,state",
    [
        (999, "CLEAR"),
        (1000, "CLEAR"),
        (1001, "PENDING"),
        (2999, "PENDING"),
        (3000, "PENDING"),
        (3001, "ACTIVE"),
    ],
)
def test_delay_boundary_preserves_original_freshness(ms, state):
    orch = Orchestrator(configuration())
    ingest(orch, 0, 1)
    result = alarm(orch, ms)
    assert result["state"] == state
    assert result["active"] == (state == "ACTIVE")
    assert result["scan_age_ms"] == ms
    assert orch.sensor_states(now_ns=ms * 10**6)[0]["data_state"] == (
        "FRESH" if ms <= 1000 else "STALE"
    )


def test_short_delay_never_activates_and_sensors_are_independent():
    orch = Orchestrator(configuration())
    assert alarm(orch, 0)["active"] is None
    ingest(orch, 0, 1)
    ingest(orch, 0, 1, "lidar-b")
    assert alarm(orch, 1800)["state"] == "PENDING"
    ingest(orch, 1900, 2)
    assert alarm(orch, 1900)["state"] == "CLEAR"
    assert orch.sensor_states(now_ns=1900 * 10**6)[0]["data_state"] == "RECOVERING"
    ingest(orch, 3000, 3)
    assert alarm(orch, 3001)["active"] is False
    assert alarm(orch, 3001, "lidar-b")["active"] is True


def test_confirmed_delay_requires_new_scans_spanning_one_second_to_clear():
    orch = Orchestrator(configuration())
    ingest(orch, 0, 1)
    # A long gap is detected even when there was no intervening status evaluation.
    ingest(orch, 3500, 2)
    assert alarm(orch, 3500)["state"] == "RECOVERING"
    assert alarm(orch, 4500)["active"] is True  # wall time alone is not recovery
    ingest(orch, 4501, 3)  # missed 1s freshness: restart preparation
    ingest(orch, 5000, 4)
    assert alarm(orch, 5000)["active"] is True
    ingest(orch, 5501, 5)
    assert alarm(orch, 5501)["state"] == "CLEAR"


def test_invalid_replays_and_old_arrivals_do_not_refresh_alarm():
    orch = Orchestrator(configuration())
    ingest(orch, 100, 1)
    assert not ingest(orch, 100, 1, received_ms=3101)
    assert not ingest(orch, 99, 2, received_ms=3101)
    assert not ingest(orch, 9999, 3, received_ms=3101)
    null = {"sensor_id": "lidar-a", "scan": None}
    assert not orch.ingest(null, now_ns=3101 * 10**6)
    assert alarm(orch, 3101)["state"] == "ACTIVE"
    ingest(orch, 200, 4, received_ms=3500)
    assert alarm(orch, 3500)["state"] == "ACTIVE"


def test_wire_extension_is_opt_in_and_does_not_change_legacy_or_judgment(tmp_path):
    from ajin_edge.v2.outbox import Outbox

    plain = Orchestrator(configuration())
    extended = Orchestrator(configuration(), scan_update_alarm_enabled=True)
    for orch in [plain, extended]:
        ingest(orch, 0, 1)
    with Outbox(tmp_path / "outbox") as store:
        stats = store.stats()
        normal = plain.status(stats, [], now_ns=1800 * 10**6)
        new = extended.status(stats, [], now_ns=1800 * 10**6)
    validate_message(normal)
    validate_message(new)
    assert "scan_update_alarm" not in normal["sensors"][0]
    assert new["sensors"][0]["scan_update_alarm"]["state"] == "PENDING"
    assert new["overall_state"] == normal["overall_state"]
    for a, b in zip(normal["sensors"], new["sensors"], strict=True):
        assert a == {k: v for k, v in b.items() if k != "scan_update_alarm"}
    assert (
        plain.assessment(now_ns=1800 * 10**6)["fill_estimate"]
        == extended.assessment(now_ns=1800 * 10**6)["fill_estimate"]
    )
    broken = deepcopy(new)
    broken["sensors"][0]["scan_update_alarm"]["active"] = True
    with pytest.raises(ValueError):
        validate_message(broken)


async def test_extended_status_survives_delivery_rpc_and_wire_schema(tmp_path):
    import json
    from pathlib import Path

    import grpc
    from ajin_edge.v2.ipc import DeliveryStub, serve_delivery
    from ajin_edge.v2.outbox import Outbox
    from jsonschema import Draft202012Validator

    orch = Orchestrator(configuration(), scan_update_alarm_enabled=True)
    ingest(orch, 0, 1)
    with Outbox(tmp_path / "outbox") as store:
        body = orch.status(store.stats(), [], now_ns=3001 * 10**6)
        schema = json.loads(
            (Path(__file__).parents[1] / "contracts/backend/v2/status.schema.json").read_text()
        )
        Draft202012Validator(schema).validate(body)
        latest = {}
        server, port = await serve_delivery(
            store,
            "127.0.0.1:0",
            site_id=body["site_id"],
            edge_id=body["edge_id"],
            latest_status=latest,
        )
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                await DeliveryStub(channel).enqueue(body)
            assert latest["payload"]["sensors"][0]["scan_update_alarm"]["active"] is True
        finally:
            await server.stop(0)
