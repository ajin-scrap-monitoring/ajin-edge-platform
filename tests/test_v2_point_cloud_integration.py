import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.outbox import Outbox
from ajin_edge.v2.processing import canonical, validate_configuration
from test_v2_independent_sections import measured_observation, tilted_configuration
from test_v2_point_cloud import point_cloud_settings


def populated(*, enabled=True, geometry_mismatch=False, count=3):
    cfg = tilted_configuration()
    if enabled is not None:
        cfg["judgment"]["point_cloud"] = {**point_cloud_settings(), "enabled": enabled}
    if count == 8:
        cfg["judgment"]["window"]["scan_count"] = 8
    obs = [
        measured_observation(cfg, i, n, 1000 - (count - n) * 100, 2000 + i * 3000)
        for n in range(1, count + 1)
        for i in range(2)
    ]
    if geometry_mismatch:
        cfg["judgment"]["sections"][0]["axis_xy"] = [0, 1]
    orch = Orchestrator(cfg)
    for o in obs:
        orch.ingest(o, now_ns=int(o["scan"]["acquired_monotonic_ns"]))
    return cfg, orch, obs


def test_opt_in_extension_preserves_original_points_and_existing_section_values():
    _, old, _ = populated(enabled=None)
    _, new, obs = populated()
    baseline = old.assessment(now_ns=1000000000)
    before = canonical(obs)
    bundle = new.assessment_bundle(now_ns=1000000000)
    body = bundle[-1]
    assert canonical(obs) == before
    for key in [
        "section_profiles",
        "fusion_result",
        "fill_estimate",
        "decision_state",
        "reason_codes",
    ]:
        assert body[key] == baseline[key]
    assert len(body["filtered_point_clouds"]) == 2
    assert all(
        c["valid_point_count"] == c["point_count"] == 3 for c in body["filtered_point_clouds"]
    )
    by_scan = {o["scan"]["scan_id"]: o for o in obs}
    for evidence in bundle[:-1]:
        sensor = evidence["sensors"][0]
        assert canonical(sensor) == canonical(by_scan[sensor["scan"]["scan_id"]])
        validate_message(evidence)
    validate_message(body)


@pytest.mark.parametrize("enabled", [None, False])
def test_disabled_extension_does_not_add_state_field(enabled):
    _, orch, _ = populated(enabled=enabled)
    assert "filtered_point_clouds" not in orch.assessment(now_ns=1000000000)


def test_cloud_only_evidence_keeps_unavailable_section_and_other_section_counts_valid():
    _, orch, _ = populated(geometry_mismatch=True)
    body = orch.assessment(now_ns=1000000000)
    assert body["section_profiles"][0]["profile"] is None
    assert body["filtered_point_clouds"][0]["valid_point_count"] == 3
    assert body["section_profiles"][1]["profile"]["sensor_stats"][0]["scan_count"] == 3
    assert len(body["evidence"]["scans"]) == 6
    validate_message(body)


def test_cloud_delay_expires_cloud_only_without_erasing_already_evaluated_section(monkeypatch):
    from ajin_edge.v2 import orchestration, point_cloud

    _, orch, _ = populated()
    clock = [1000000000]
    monkeypatch.setattr(orchestration.time, "monotonic_ns", lambda: clock[0])
    original = point_cloud.build_cloud

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 3000000000
        return result

    monkeypatch.setattr(point_cloud, "build_cloud", delayed)
    body = orch.assessment()
    assert all(r["profile"] is not None for r in body["section_profiles"])
    assert body["fill_estimate"] is not None
    assert "INPUT_EXPIRED_DURING_JUDGMENT" not in body["reason_codes"]
    assert all(c["valid_point_count"] == 0 for c in body["filtered_point_clouds"])
    assert all(
        "INPUT_EXPIRED_DURING_FILTER" in c["reason_codes"] for c in body["filtered_point_clouds"]
    )
    validate_message(body)


def test_first_sensor_cloud_is_rechecked_when_second_sensor_finishes(monkeypatch):
    from ajin_edge.v2 import orchestration, point_cloud

    _, orch, _ = populated()
    clock = [1000000000]
    monkeypatch.setattr(orchestration.time, "monotonic_ns", lambda: clock[0])
    original = point_cloud.build_cloud

    def delayed(configuration, sensor, observations, **kwargs):
        result = original(configuration, sensor, observations, **kwargs)
        clock[0] = 1700000000 if sensor["sensor_id"] == "lidar-a" else 2000000000
        return result

    monkeypatch.setattr(point_cloud, "build_cloud", delayed)
    body = orch.assessment()
    assert all(c["valid_point_count"] == 0 for c in body["filtered_point_clouds"])
    assert body["fill_estimate"] is not None
    validate_message(body)


def test_eight_scan_history_still_fits_existing_durable_bundle_limit(tmp_path):
    _, orch, _ = populated(count=8)
    bundle = orch.assessment_bundle(now_ns=1000000000)
    assert len(bundle) == 17
    with Outbox(tmp_path) as store:
        store.enqueue_bundle(bundle)
        assert store.stats()["pending_messages"] == 17


@pytest.mark.parametrize(
    "field,value",
    [
        ("valid", []),
        ("xyz_mm", []),
        ("valid_point_count", 99),
        ("point_count", 99),
        ("source_scans", []),
        ("sample_counts", [9, 9, 9]),
        ("point_indices", [0, 0, 0]),
        ("xyz_mm", [[float("nan"), 0, 0]] * 3),
        ("valid", [False, False, False]),
        ("verification_state", "UNVERIFIED"),
    ],
)
def test_cloud_wire_rejects_broken_vector_or_evidence_invariants(field, value):
    _, orch, _ = populated()
    body = orch.assessment(now_ns=1000000000)
    body["filtered_point_clouds"][0][field] = value
    with pytest.raises(ValueError):
        validate_message(body)


def test_wire_rejects_numeric_unverified_cloud_even_when_marked_limited():
    _, orch, _ = populated()
    body = orch.assessment(now_ns=1000000000)
    cloud = body["filtered_point_clouds"][0]
    cloud.update(verification_state="UNVERIFIED", state="LIMITED")
    with pytest.raises(ValueError):
        validate_message(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("spatial_radius_points", -1),
        ("spatial_min_neighbors", 99),
        ("distance_min_mm", 40000),
        ("processing_version", "unknown"),
        ("enabled", "yes"),
        ("spatial_radius_points", 2.0),
        ("spatial_min_neighbors", 3.0),
        ("quality_min", 10.0),
    ],
)
def test_invalid_filter_configuration_is_rejected(field, value):
    cfg = tilted_configuration()
    cfg["judgment"]["point_cloud"] = point_cloud_settings()
    cfg["judgment"]["point_cloud"][field] = value
    with pytest.raises(ValueError):
        validate_configuration(cfg)
