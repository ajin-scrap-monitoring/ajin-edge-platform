"""SDK wire adapter: use driver-assigned IDs and timestamps, never receipt time here."""

from datetime import UTC, datetime

from .processing import validate_scan


def from_frame(frame, *, edge_id, sensor_id, revision):
    if (
        frame.schema_version != "2.0"
        or frame.edge_id != edge_id
        or frame.sensor_id != sensor_id
        or frame.config_revision != revision
        or frame.sdk_status != "OK"
        or not frame.scan_id
        or not frame.clock_domain_id
    ):
        raise ValueError("incompatible driver identity or missing v2 acquisition metadata")
    raw = {
        "sensor_id": frame.sensor_id,
        "scan_id": frame.scan_id,
        "stream_instance_id": frame.instance_id,
        "sequence": frame.sequence,
        "acquired_at": datetime.fromtimestamp(frame.acquired_at_unix_ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "timestamp_source": "EDGE_SCAN_RECEIVED",
        "clock_domain_id": frame.clock_domain_id,
        "acquired_monotonic_ns": str(frame.acquired_monotonic_ns),
        "clock_state": "UNSYNCED",
        "clock_offset_ms": None,
        "samples": [
            {
                "angle_mdeg": p.angle_mdeg,
                "distance_mm": p.distance_mm,
                "quality": p.quality,
                **({"sdk_invalid_range": True} if p.sdk_invalid_range else {}),
            }
            for p in frame.samples
        ],
    }
    validate_scan(raw)
    return raw
