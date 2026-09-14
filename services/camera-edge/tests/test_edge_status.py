import json

from camera_edge.main import StreamStats
from camera_edge.status import CameraStatusWriter


def test_status_only_reports_healthy_after_recent_successful_frame(tmp_path):
    writer = CameraStatusWriter(tmp_path, "edge-1", "rev-1", "camera-1")
    stats = StreamStats()
    writer.update(stats.snapshot())
    path = tmp_path / "camera-edge.json"
    assert json.loads(path.read_text())["state"] == "STARTING"
    stats.record_send(10)
    stats.camera_state = "streaming"
    stats.websocket_state = "authenticated"
    writer.update(stats.snapshot())
    assert json.loads(path.read_text())["state"] == "HEALTHY"
    snapshot = stats.snapshot()
    snapshot["last_send_age_seconds"] = 6
    writer.update(snapshot)
    assert json.loads(path.read_text())["state"] == "DEGRADED"


def test_camera_snapshot_joins_edge_identity_without_changing_media_payload(tmp_path):
    writer = CameraStatusWriter(
        tmp_path, "edge-1", "rev-1", "camera-1", site_id="site-1", deployment_revision="release-1"
    )
    writer.update(
        {"websocket_state": "fatal", "camera_state": "streaming", "last_send_age_seconds": 0}
    )
    value = json.loads((tmp_path / "camera-edge.json").read_text())
    assert value["service"] == "camera-edge"
    assert value["edge_id"] == "edge-1"
    assert value["config_revision"] == "rev-1"
    assert value["site_id"] == "site-1"
    assert value["deployment_revision"] == "release-1"
    assert value["state"] == "FATAL"
    assert value["reason_codes"] == ["CAMERA_AUTHENTICATION_FAILED"]
    assert "camera_token" not in value
    assert not list(tmp_path.glob("*.tmp"))
