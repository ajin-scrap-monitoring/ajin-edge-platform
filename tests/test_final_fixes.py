import json
from types import SimpleNamespace

import pytest
from ajin_edge.config import load_config
from ajin_edge_orchestrator.aggregate import aggregate, meaningful_signature
from ajin_measurement_uplink.outbox import Outbox
from test_orchestrator import statuses
from test_processing import config, frame


def test_camera_and_release_identity_mismatch():
    c = dict(
        site_id="site-1",
        edge_id="edge-1",
        config_revision="rev-1",
        camera_id="cam-a",
        deployment_revision="release-1",
        service_versions={"camera-edge": "0.1.0"},
    )
    services = statuses()
    for item in services:
        item.update(site_id="site-1", deployment_revision="release-1", service_version="0.1.0")
    services[-1]["camera_id"] = "cam-a"
    good = aggregate(c, services, dict(state="SYNCED", offset_ms=0))
    assert good["state"] == "HEALTHY"
    services[-1]["camera_id"] = "cam-b"
    bad = aggregate(c, services, dict(state="SYNCED", offset_ms=0))
    assert bad["state"] == "CONFIG_ERROR"
    assert "CONFIG_CAMERA_MISMATCH" in bad["reason_codes"]
    assert meaningful_signature(good) != meaningful_signature(bad)
    services[-1]["camera_id"] = "cam-a"
    services[0]["site_id"] = "other"
    services[1]["deployment_revision"] = "other"
    services[-1]["service_version"] = "9.0"
    bad = aggregate(c, services, dict(state="SYNCED", offset_ms=0))
    assert {
        "CONFIG_SITE_MISMATCH",
        "CONFIG_DEPLOYMENT_MISMATCH",
        "CONFIG_SERVICE_VERSION_MISMATCH",
    } <= set(bad["reason_codes"])


def test_camera_config_identifier_validated(tmp_path):
    c = config()
    c["camera_id"] = "bad/id"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(c))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["lidar-processing", "measurement-uplink", "edge-orchestrator"])
@pytest.mark.parametrize("failure", ["checksum", "malformed"])
async def test_config_failure_status_uses_only_environment(tmp_path, monkeypatch, service, failure):
    for key, value in dict(
        SITE_ID="trusted-site",
        EDGE_ID="trusted-edge",
        CONFIG_REVISION="trusted-r",
        DEPLOYMENT_REVISION="trusted-release",
        CAMERA_ID="trusted-camera",
    ).items():
        monkeypatch.setenv(key, value)
    cfg = tmp_path / "edge.json"
    cfg.write_text("{broken" if failure == "malformed" else json.dumps(config()))
    if failure == "checksum":
        monkeypatch.setenv("CONFIG_SHA256", "0" * 64)
    path = tmp_path / f"{service}.json"
    path.write_text(json.dumps({"state": "HEALTHY"}))
    args = SimpleNamespace(config=cfg, status_dir=tmp_path, own_status_dir=tmp_path)
    if service == "lidar-processing":
        from ajin_lidar_processing.runtime import run as start
    elif service == "measurement-uplink":
        from ajin_measurement_uplink.runtime import serve as start
    else:
        from ajin_edge_orchestrator.runtime import serve as start
    with pytest.raises(ValueError):
        await start(args)
    result = json.loads(path.read_text())
    assert result["state"] == "FATAL"
    assert result["edge_id"] == "trusted-edge" and result["site_id"] == "trusted-site"
    assert result["deployment_revision"] == "trusted-release"
    assert result["reason_codes"] == [
        "CONFIG_CHECKSUM_MISMATCH" if failure == "checksum" else "CONFIG_INVALID"
    ]


def test_outbox_monotonic_retry_restart_retention_and_loss_range(tmp_path, measurement):
    clock = SimpleNamespace(wall=10000.0, mono=100.0)
    path = tmp_path / "out.db"

    def factory():
        return Outbox(
            path,
            max_age_seconds=100,
            wall_clock=lambda: clock.wall,
            monotonic_clock=lambda: clock.mono,
        )

    with factory() as out:
        out.enqueue(measurement)
        out.retry(measurement["measurement_id"], next_attempt=10060)
        clock.wall -= 3600
        clock.mono += 61
        assert out.next_ready() is not None
        out.retry(measurement["measurement_id"], next_attempt=clock.wall + 10000)
    with factory() as out:
        clock.wall += 100000
        clock.mono += 60
        assert out.next_ready() is not None
        # Offline time is not trusted elapsed retention; running elapsed is.
        out.maintain()
        assert out.stats()["pending"] == 0
        stats = out.stats()
        assert stats["first_lost_id"] == measurement["measurement_id"]
        assert stats["loss_range_start_unix"] == 10000
        assert stats["loss_range_end_unix"] == 10000
    with factory() as out:
        assert out.stats()["first_lost_id"] == measurement["measurement_id"]


def test_outbox_oldest_pending_age_does_not_jump_with_utc(tmp_path, measurement):
    clock = SimpleNamespace(wall=1000.0, mono=10.0)
    with Outbox(
        tmp_path / "db", wall_clock=lambda: clock.wall, monotonic_clock=lambda: clock.mono
    ) as out:
        out.enqueue(measurement)
        clock.wall += 100000
        clock.mono += 5
        stats = out.stats()
        assert stats["oldest_pending_unix"] == 1000
        assert stats["oldest_pending_age_seconds"] == 5


def test_explicit_candidate_hints_and_invalid_reset():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    c["calibration"]["candidate_hints"] = dict(
        window_seconds=2,
        rapid_rise_ratio=0.2,
        collection_drop_ratio=0.2,
        coverage_drop_ratio=0.2,
        occlusion_change_mm=20,
        occlusion_width_mm=[50, 100],
        occlusion_shift_mm=[50, 150],
    )
    e = ProcessingEngine(c)

    def measure(seq, h):
        for sid in ["a", "b"]:
            e.ingest(frame(sid, seq, seq * 1_000_000_000, h))
        return e.measure(seq * 1_000_000_000, 100000 + seq * 1000, {})

    measure(1, 10)
    m = measure(2, 90)
    assert "RAPID_RISE_SUSPECTED" in m["quality"]["reason_codes"]
    # Expire old profile and establish a new baseline before the drop.
    measure(5, 90)
    m = measure(6, 10)
    assert "COLLECTION_DROP_SUSPECTED" in m["quality"]["reason_codes"]
    assert e.measure(9_000_000_000, 109000, {})["quality"]["state"] == "INVALID"
    m = measure(10, 90)
    assert "RAPID_RISE_SUSPECTED" not in m["quality"]["reason_codes"]


def hint_config():
    c = config()
    c["calibration"]["candidate_hints"] = dict(
        window_seconds=2,
        rapid_rise_ratio=0.2,
        collection_drop_ratio=0.2,
        coverage_drop_ratio=0.2,
        occlusion_change_mm=20,
        occlusion_width_mm=[50, 100],
        occlusion_shift_mm=[50, 150],
    )
    return c


def test_moving_narrow_structure_marks_candidate_without_deleting_returns():
    from ajin_lidar_processing.engine import ProcessingEngine

    e = ProcessingEngine(hint_config())
    for seq in range(1, 4):
        for sid in ["a", "b"]:
            f = frame(sid, seq, seq * 100_000_000)
            raised = frame(sid, seq, seq * 100_000_000, height=90)
            f.samples[seq - 1] = raised.samples[seq - 1]
            e.ingest(f)
    m = e.measure(300_000_000, 100300, {})
    assert "OCCLUSION_SUSPECTED" in m["quality"]["reason_codes"]
    assert m["quality"]["state"] == "DEGRADED"
    assert m["sensors"][0]["coverage_ratio"] == 1


def test_coverage_drop_candidate_and_calibration_reset():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = hint_config()
    e = ProcessingEngine(c)
    for sid in ["a", "b"]:
        e.ingest(frame(sid))
    e.measure(1_000_000_000, 101000, {})
    # Five fresh scans make the missing interior column observable, not historical.
    for seq in range(2, 7):
        for sid in ["a", "b"]:
            f = frame(sid, seq, 1_000_000_000 + seq * 100_000_000)
            del f.samples[1]
            e.ingest(f)
    m = e.measure(1_600_000_000, 101600, {})
    assert "OCCLUSION_SUSPECTED" in m["quality"]["reason_codes"]
    assert e.measure(1_600_000_000, 101600, {}) is None


def test_hints_require_complete_explicit_calibrated_rules():
    from ajin_lidar_processing.engine import ProcessingEngine

    c = config()
    c["calibration"]["candidate_hints"] = {"rapid_rise_ratio": 0.2}
    with pytest.raises(ValueError):
        ProcessingEngine(c)


def test_terminal_config_diagnostic_survives_staleness(tmp_path):
    from datetime import UTC, datetime, timedelta

    from ajin_edge.status import StatusWriter, read_status

    writer = StatusWriter(
        tmp_path,
        "lidar-processing",
        "edge-1",
        "rev-1",
        site_id="site-1",
        deployment_revision="release-1",
    )
    writer.write("FATAL", ["CONFIG_CHECKSUM_MISMATCH"], camera_id="cam-a")
    diagnostic = read_status(writer.path, now=datetime.now(UTC) + timedelta(hours=1))
    assert diagnostic["state"] == "FATAL"
    assert diagnostic["diagnostic_stale"] is True
    services = statuses()
    services[2] = diagnostic
    result = aggregate(
        dict(site_id="site-1", edge_id="edge-1", config_revision="rev-1"),
        services,
        dict(state="SYNCED", offset_ms=0),
    )
    assert result["state"] == "CONFIG_ERROR"


def test_deployment_example_manifest_and_heartbeat_schema():
    from pathlib import Path

    import jsonschema

    root = Path(__file__).resolve().parents[1]
    example = load_config(root / "deploy/config.example/processing.example.json")
    assert len(example["service_versions"]) == 6
    snapshot = aggregate(example, [], dict(state="UNSYNCED", offset_ms=None))
    schema = json.loads((root / "contracts/backend/v1/edge-heartbeat.schema.json").read_text())
    jsonschema.validate(snapshot, schema)


@pytest.mark.asyncio
async def test_worker_retry_and_report_ticks_ignore_backward_utc(tmp_path, measurement):
    from ajin_measurement_uplink.runtime import DeliveryWorker

    clock = SimpleNamespace(wall=10000.0, mono=100.0)

    class Transport:
        calls = 0

        async def send(self, payload):
            self.calls += 1
            return SimpleNamespace(kind="retry", reason="NETWORK", retry_after=60)

    transport = Transport()
    with Outbox(
        tmp_path / "worker.db", wall_clock=lambda: clock.wall, monotonic_clock=lambda: clock.mono
    ) as out:
        out.enqueue(measurement)
        worker = DeliveryWorker(out, transport, None)
        await worker.step(now=clock.wall, monotonic_now=clock.mono)
        clock.wall -= 3600
        clock.mono += 59
        out.maintain()
        assert out.stats()["oldest_pending_age_seconds"] == 59
        assert not await worker.step(now=clock.wall, monotonic_now=clock.mono)
        clock.mono += 1
        assert await worker.step(now=clock.wall, monotonic_now=clock.mono)
        assert transport.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "quarantine"])
async def test_slow_delivery_cannot_double_age_concurrent_pending(tmp_path, measurement, outcome):
    import asyncio

    from ajin_measurement_uplink.runtime import DeliveryWorker

    clock = SimpleNamespace(wall=10000.0, mono=100.0)
    entered, release = asyncio.Event(), asyncio.Event()

    class Transport:
        async def send(self, payload):
            assert payload["measurement_id"] == "sending"
            entered.set()
            await release.wait()
            return SimpleNamespace(kind=outcome, reason="HTTP_422")

    with Outbox(
        tmp_path / "concurrent.db",
        max_age_seconds=6,
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.mono,
    ) as out:
        out.enqueue({**measurement, "measurement_id": "survivor"})
        out.enqueue({**measurement, "measurement_id": "sending"})
        worker = DeliveryWorker(out, Transport(), None)
        sending = asyncio.create_task(worker.step(now=clock.wall, monotonic_now=clock.mono))
        await entered.wait()
        clock.wall += 4
        clock.mono += 4
        out.maintain()
        assert out.stats()["oldest_pending_age_seconds"] == 4
        out.enqueue({**measurement, "measurement_id": "newcomer"})
        release.set()
        assert await sending
        assert out.stats()["oldest_pending_age_seconds"] == 4
        # A stale explicit sample from any other async caller is harmless too.
        out.next_ready(now=10000, monotonic_now=100)
        assert out.stats()["oldest_pending_age_seconds"] == 4
        out.maintain()
        assert out.stats()["pending"] == 2
        assert out.stats()["lost_records"] == (outcome == "quarantine")
        clock.wall += 2
        clock.mono += 2
        out.maintain()
        assert out.stats()["pending"] == 1
        assert out.next_ready()["measurement_id"] == "newcomer"
        assert out.stats()["oldest_pending_age_seconds"] == 2
