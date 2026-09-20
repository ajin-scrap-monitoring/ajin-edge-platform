import pytest
from ajin_edge.v2.adapter import from_frame
from ajin_edge.wire import lidar_pb2


def test_adapter_does_not_invent_identity_or_retimestamp():
    frame = lidar_pb2.ScanFrame(
        schema_version="2.0",
        edge_id="edge-test",
        sensor_id="lidar-a",
        instance_id="stream-test",
        sequence=3,
        acquired_at_unix_ms=1000,
        acquired_monotonic_ns=123456,
        config_revision="cfg-2",
        sdk_status="OK",
    )
    with pytest.raises(ValueError):
        from_frame(frame, edge_id="edge-test", sensor_id="lidar-a", revision="cfg-2")
    frame.scan_id = "driver-assigned-test"
    frame.clock_domain_id = "boot-test"
    frame.samples.add(angle_mdeg=12345, distance_mm=456, quality=0)
    raw = from_frame(frame, edge_id="edge-test", sensor_id="lidar-a", revision="cfg-2")
    assert raw["scan_id"] == frame.scan_id
    assert raw["acquired_monotonic_ns"] == "123456"
    assert raw["acquired_at"] == "1970-01-01T00:00:01.000Z"
    assert raw["samples"] == [{"angle_mdeg": 12345, "distance_mm": 456, "quality": 0}]
