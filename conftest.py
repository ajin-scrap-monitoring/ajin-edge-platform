"""Shared hand-authored measurement fixture for unit and local integration tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest


@pytest.fixture
def measurement():
    return {
        "schema_version": "1.0",
        "measurement_id": str(uuid4()),
        "measurement_cycle_id": str(uuid4()),
        "site_id": "site-1",
        "edge_id": "edge-1",
        "measured_at": datetime.now(UTC).isoformat(),
        "calibration_version": "cal-1",
        "config_revision": "rev-1",
        "fill_ratio": 0.5,
        "fill_percent": 50.0,
        "quality": {"state": "GOOD", "confidence": 0.9, "reason_codes": []},
        "sensors": [
            {
                "sensor_id": "lidar-a",
                "sequence": 1,
                "section_fill_ratio": 0.5,
                "median_height_mm": 500,
                "p90_height_mm": 500,
                "valid_sample_ratio": 1,
                "coverage_ratio": 1,
                "state": "GOOD",
            }
        ],
    }
