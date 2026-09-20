from copy import deepcopy

import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.processing import ProcessingEngine
from test_v2_processing import configuration, scan


def message():
    engine = ProcessingEngine(configuration(), clock_domain_id="boot-test")
    engine.ingest(scan())
    return engine.periodic(now_ns=1000000000)


def test_valid_measurement_and_empty_measurement():
    validate_message(message())
    validate_message(
        ProcessingEngine(configuration(), clock_domain_id="boot-test").periodic(now_ns=1000000000)
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("sensors", 0, "scan", "selected_point_count"), 999),
        (("sensors", 0, "scan", "points", 0, "index"), 2),
        (("sensors", 0, "scan", "points", 0, "position_mm", "z"), 0.5),
        (("sensors", 0, "scan", "acquired_monotonic_ns"), 950000000),
        (("sensors", 0, "availability"), "NO_NEW_SCAN"),
        (("pair_state",), "MATCHED"),
        (("schema_version",), "1.0"),
    ],
)
def test_malformed_measurement_rejected(path, value):
    body = message()
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_message(body)


def test_evidence_keeps_original_scan_and_allows_old_timestamp():
    body = message()
    body.update(
        delivery_kind="EVIDENCE",
        selection_policy="REFERENCED_SCANS",
        report_interval_ms=None,
        pair_state="NOT_APPLICABLE",
        pair_delta_ms=None,
        assessment_ids=["event-test"],
    )
    body["sensors"] = body["sensors"][:1]
    validate_message(body)
    bad = deepcopy(body)
    bad["assessment_ids"] = []
    with pytest.raises(ValueError):
        validate_message(bad)


@pytest.mark.parametrize(
    "mutation",
    [
        {"quality": 256},
        {"quality": True},
        {"extra": 1},
        {"position_mm": [1, 2, 3]},
        {"position_mm": {"x": 0, "y": 0, "z": float("nan")}},
        {"calculation_reason_codes": ["X", "X"]},
        {"distance_mm": -1},
    ],
)
def test_point_array_fast_validation_matches_public_schema(mutation):
    body = message()
    body["sensors"][0]["scan"]["points"][0].update(mutation)
    with pytest.raises(ValueError):
        validate_message(body)
