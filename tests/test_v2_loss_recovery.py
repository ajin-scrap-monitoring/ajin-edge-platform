import pytest
from ajin_edge.v2.loss_recovery import OutboxLossRecovery


def tick(tracker, now, count=7, healthy=True, epoch="same", missing=False):
    return tracker.observe(count, healthy=healthy, epoch=epoch, now=now, evidence_missing=missing)


def advance(tracker, start, end, **kwargs):
    for t in range(start, end + 1):
        active = tick(tracker, t, **kwargs)
    return active


def test_existing_loss_requires_full_continuously_observed_hour():
    t = OutboxLossRecovery()
    assert advance(t, 0, 3599)
    assert not tick(t, 3600)
    assert t.snapshot(3600)["loss_high_water"] == 7
    assert tick(t, 3601, count=8)
    assert advance(t, 3602, 7200, count=8)
    assert not tick(t, 7201, count=8)


@pytest.mark.parametrize("break_kind", ["unhealthy", "gap", "restart", "decrease", "missing"])
def test_break_never_counts_unknown_time_as_normal(break_kind):
    t = OutboxLossRecovery()
    advance(t, 0, 3590)
    args = {}
    if break_kind == "unhealthy":
        args["healthy"] = False
    if break_kind == "restart":
        args["epoch"] = "new"
    if break_kind == "decrease":
        args["count"] = 0
    if break_kind == "missing":
        args["missing"] = True
    at = 3600 if break_kind == "gap" else 3591
    assert tick(t, at, **args)
    assert tick(t, at + 1)
    assert t.snapshot(at + 1)["continuous_healthy_seconds"] <= 1
    assert t.snapshot(at + 1)["loss_high_water"] == 7


def test_restart_keeps_history_but_never_credits_offline_time():
    t = OutboxLossRecovery()
    assert not advance(t, 0, 3600)
    restarted = OutboxLossRecovery(previous_high_water=t.snapshot(3600)["loss_high_water"])
    assert tick(restarted, 9000, count=0)
    assert restarted.snapshot(9000)["continuous_healthy_seconds"] == 0


def test_other_fault_after_recovery_does_not_erase_history_or_invent_new_loss():
    t = OutboxLossRecovery()
    assert not advance(t, 0, 3600)
    assert not tick(t, 3601, healthy=False)
    assert tick(t, 3602, missing=True)


def test_no_historical_loss_never_creates_loss_warning():
    t = OutboxLossRecovery()
    assert not tick(t, 0, count=0, healthy=False)


def test_orchestrator_clears_only_loss_reason_and_retains_storage_history(monkeypatch):
    from ajin_edge.v2.orchestration import Orchestrator
    from test_v2_processing import configuration

    orch = Orchestrator(configuration())
    monkeypatch.setattr(
        orch, "sensor_states", lambda **kw: [{"data_state": "FRESH", "reason_codes": []}]
    )
    stats = dict(
        pending_messages=500,
        pending_bytes=1234,
        quarantined_messages=0,
        last_backend_ack_at=None,
        limit_bytes=9999,
        used_bytes=4000,
        lost_messages=7,
        loss_from=None,
        loss_to=None,
        last_loss_reason="CAPACITY_EVICTION",
        unavailable_evidence_scan_ids=[],
    )
    services = [
        {"service": k, "state": "HEALTHY"}
        for k in [
            "lidar-driver-a",
            "lidar-driver-b",
            "lidar-processing",
            "measurement-uplink",
            "edge-orchestrator",
        ]
    ]
    context = {"healthy": True, "epoch": "stable"}
    for t in range(3601):
        orch.recovery_evidence = (t * 10**9, frozenset())
        body = orch.status(stats, services, now_ns=t * 10**9, recovery_context=context)
    assert "OUTBOX_DATA_LOSS" not in body["reason_codes"]
    assert body["storage"]["lost_messages"] == 7
    assert body["delivery"]["pending_messages"] == 500
    services.append({"service": "camera-edge", "state": "FATAL"})
    body = orch.status(stats, services, now_ns=3601 * 10**9, recovery_context=context)
    assert body["overall_state"] == "DEGRADED"
    assert "OUTBOX_DATA_LOSS" not in body["reason_codes"]
    orch.recovery_evidence = (3602 * 10**9, frozenset({"missing-scan"}))
    stats["unavailable_evidence_scan_ids"] = ["missing-scan"]
    body = orch.status(stats, services, now_ns=3602 * 10**9, recovery_context=context)
    assert "OUTBOX_DATA_LOSS" in body["reason_codes"]


async def test_ack_freshness_proof_cannot_be_refreshed_by_old_backlog(monkeypatch):
    from ajin_edge.v2.transport import DeliveryClient, Result

    c = DeliveryClient("https://backend.example", "test", None)

    async def accepted(payload):
        return Result("accepted", received_at="2026-11-11T00:00:01Z")

    monkeypatch.setattr(c, "_send", accepted)
    body = dict(
        message_id="one",
        message_type="lidar_measurement",
        delivery_kind="PERIODIC",
        generated_at="2026-11-11T00:00:00Z",
    )
    body["sensors"] = [
        {"sensor_id": sid, "scan": {"acquired_at": body["generated_at"]}}
        for sid in ["lidar-a", "lidar-b"]
    ]
    await c.send(body)
    assert set(c.metrics["PERIODIC"]["latest_accepted_sensors"]) == {"lidar-a", "lidar-b"}
    assert c.metrics["PERIODIC"]["latest_accepted_generated_at"] == body["generated_at"]
    await c.send({**body, "generated_at": "2026-11-10T00:00:00Z"})
    assert c.metrics["PERIODIC"]["latest_accepted_generated_at"] == body["generated_at"]


@pytest.mark.parametrize(
    "fault",
    ["none", "old_payload", "stale_status", "no_ack", "foreign", "missing", "missing_b", "old_b"],
)
def test_recovery_requires_recent_current_payload_acks(tmp_path, fault):
    import json
    from datetime import UTC, datetime

    from ajin_edge.v2.runtime import recovery_delivery_healthy

    now = datetime(2026, 11, 11, tzinfo=UTC)
    stamp = now.isoformat()
    config = {
        "status_dir": str(tmp_path),
        "configuration": {"site_id": "s", "edge_id": "e", "config_revision": "r"},
    }
    source = {
        **config["configuration"],
        "reported_at": stamp,
        "state": "HEALTHY",
        "http_delivery": {
            k: {"latest_accepted_generated_at": stamp, "latest_accepted_observed_at": stamp}
            for k in ["PERIODIC", "STATE"]
        },
    }
    config["configuration"]["sensors"] = [{"sensor_id": k} for k in ["lidar-a", "lidar-b"]]
    source["http_delivery"]["PERIODIC"]["latest_accepted_sensors"] = {
        k: {"generated_at": stamp, "observed_at": stamp, "acquired_at": stamp}
        for k in ["lidar-a", "lidar-b"]
    }
    if fault == "missing_b":
        del source["http_delivery"]["PERIODIC"]["latest_accepted_sensors"]["lidar-b"]
    if fault == "old_b":
        source["http_delivery"]["PERIODIC"]["latest_accepted_sensors"]["lidar-b"]["acquired_at"] = (
            "2026-11-10T00:00:00Z"
        )
    if fault == "old_payload":
        source["http_delivery"]["PERIODIC"]["latest_accepted_generated_at"] = "2026-11-10T00:00:00Z"
    if fault == "stale_status":
        source["reported_at"] = "2026-11-10T00:00:00Z"
    if fault == "no_ack":
        source["http_delivery"].pop("STATE")
    if fault == "foreign":
        source["edge_id"] = "another"
    if fault != "missing":
        (tmp_path / "measurement-uplink.json").write_text(json.dumps(source))
    assert recovery_delivery_healthy(config, now=now) == (fault == "none")


def test_checkpoint_keeps_high_water_only_for_same_device(tmp_path):
    import json

    from ajin_edge.v2.runtime import previous_loss_high_water

    config = {"status_dir": str(tmp_path), "configuration": {"site_id": "s", "edge_id": "e"}}
    path = tmp_path / "edge-orchestrator.json"
    source = {
        "site_id": "s",
        "edge_id": "e",
        "loss_recovery": {
            "loss_high_water": 6912,
            "continuous_healthy_seconds": 9000,
            "active": False,
        },
    }
    path.write_text(json.dumps(source))
    t = OutboxLossRecovery(previous_loss_high_water(config))
    assert tick(t, 1, count=0)
    assert t.snapshot(1)["loss_high_water"] == 6912
    assert t.snapshot(1)["continuous_healthy_seconds"] == 0
    source["edge_id"] = "foreign"
    path.write_text(json.dumps(source))
    assert previous_loss_high_water(config) == 0
    path.write_text("{broken")
    assert previous_loss_high_water(config) == 0
