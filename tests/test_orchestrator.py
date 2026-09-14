from ajin_edge_orchestrator.aggregate import aggregate, meaningful_signature


def statuses():
    return [
        {
            "service": name,
            "state": "HEALTHY",
            "edge_id": "edge-1",
            "site_id": "site-1",
            "config_revision": "rev-1",
            "reason_codes": [],
        }
        for name in (
            "lidar-driver-a",
            "lidar-driver-b",
            "lidar-processing",
            "measurement-uplink",
            "camera-edge",
        )
    ]


def heartbeat(services, clock=None):
    return aggregate(
        {"site_id": "site-1", "edge_id": "edge-1", "config_revision": "rev-1"},
        services,
        clock or {"state": "SYNCED", "offset_ms": 0},
    )


def test_camera_failure_does_not_mark_measurement_unavailable():
    services = statuses()
    services[-1]["state"] = "UNKNOWN"
    assert heartbeat(services)["state"] == "DEGRADED"
    services[0]["state"] = "RETRYING"
    assert heartbeat(services)["state"] == "DEGRADED"
    services[1]["state"] = "RETRYING"
    assert heartbeat(services)["state"] == "MEASUREMENT_UNAVAILABLE"


def test_mixed_revision_or_wrong_edge_is_config_error():
    services = statuses()
    services[0]["config_revision"] = "wrong"
    assert heartbeat(services)["state"] == "CONFIG_ERROR"
    services[0]["config_revision"] = "rev-1"
    services[0]["edge_id"] = "another-edge"
    assert heartbeat(services)["state"] == "CONFIG_ERROR"


def test_uplink_failure_reports_buffering_and_keeps_sensor_details():
    services = statuses()
    services[3]["state"] = "RETRYING"
    result = heartbeat(services)
    assert result["state"] == "OFFLINE_BUFFERING"
    assert result["services"][0]["state"] == "HEALTHY"


def test_unknown_clock_degrades_healthy_edge_and_timestamps_do_not_trigger_event():
    result = heartbeat(statuses(), {"state": "UNSYNCED", "offset_ms": None})
    assert result["state"] == "DEGRADED"
    before = meaningful_signature(result)
    result["reported_at"] = "new"
    result["services"][0]["reported_at"] = "new"
    result["services"][0]["scans"] = 100
    assert meaningful_signature(result) == before
    result["services"][0]["state"] = "RETRYING"
    assert meaningful_signature(result) != before
