import importlib
import json
from copy import deepcopy

import pytest
from ajin_edge.v2.contracts import validate_message
from ajin_edge.v2.processing import envelope, transform_scan, utc_now
from test_v2_processing import configuration, scan


def control_module():
    name = "ajin_edge.v2.configuration_state"
    assert importlib.util.find_spec(name) is not None, "calibration control is not implemented"
    return importlib.import_module(name)


def base_runtime(tmp_path):
    cfg = configuration()
    cfg["coordinate_frame"].update(
        unit="mm",
        origin_description="Test",
        x_axis_description="X",
        y_axis_description="Y",
        z_axis_description="Z up",
        handedness="RIGHT_HANDED",
    )
    cfg.update(envelope(cfg, "edge_configuration"))
    for sensor in cfg["sensors"]:
        sensor["calibration"] = None
    (tmp_path / "status").mkdir()
    (tmp_path / "control").mkdir()
    (tmp_path / "boot").write_text("boot-test")
    return {
        "configuration": cfg,
        "driver_config_revision": cfg["config_revision"],
        "configuration_state_file": str(tmp_path / "control/calibration-control.json"),
        "control_dir": str(tmp_path / "control"),
        "status_dir": str(tmp_path / "status"),
        "clock_domain_file": str(tmp_path / "boot"),
    }


def calibration_request(base):
    cfg = base["configuration"]
    return {
        "site_id": cfg["site_id"],
        "edge_id": cfg["edge_id"],
        "base_config_revision": cfg["config_revision"],
        "config_revision": "cal-update-1",
        "coordinate_frame_id": cfg["coordinate_frame"]["frame_id"],
        "calibrations": [
            {
                "sensor_id": "lidar-a",
                "calibration": {
                    "version": "installation-v1",
                    "verification_state": "VERIFIED",
                    "angle_sign": 1,
                    "angle_zero_offset_mdeg": 0,
                    "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    "translation_mm": [0, 0, 1000],
                },
            }
        ],
    }


class Delivery:
    acked = False
    state = "pending"

    def __init__(self):
        self.messages = []

    async def enqueue(self, body):
        validate_message(body)
        self.messages.append(deepcopy(body))

    async def inspect(self, mid):
        return {"message_id": mid, "remote_acked": self.acked, "state": self.state}


async def test_calibration_is_durable_but_inactive_until_remote_ack(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    original = deepcopy(base)
    store = module.ControlState(base)
    reply = store.propose(calibration_request(base), "request-1")
    assert reply["state"] == "PENDING_BACKEND_ACK"
    assert store.snapshot()["config_revision"] == base["configuration"]["config_revision"]
    store.close()
    store = module.ControlState(base)
    try:
        client = Delivery()
        await store.advance(client)
        assert store.job("request-1")["state"] == "PENDING_BACKEND_ACK"
        client.acked = True
        await store.advance(client)
        assert store.snapshot()["config_revision"] == "cal-update-1"
        assert store.job("request-1")["state"] == "APPLYING"
        effective = module.effective_runtime(base)
        assert effective["driver_config_revision"] == base["driver_config_revision"]
        assert effective["configuration"]["sensors"][0]["calibration"]["translation_mm"][2] == 1000
        assert effective["configuration"]["sensors"][1]["calibration"] is None
        projected = transform_scan(scan(), effective["configuration"]["sensors"][0])
        assert projected["scan"]["points"][0]["position_mm"]["z"] == 1000
        assert len({m["message_id"] for m in client.messages}) == 1
        assert base == original
    finally:
        store.close()


async def test_backend_schema_rejection_keeps_previous_configuration(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as store:
        store.propose(calibration_request(base), "request-1")
        client = Delivery()
        client.state = "quarantine"
        await store.advance(client)
        assert store.job("request-1")["state"] == "REJECTED"
        assert store.snapshot()["config_revision"] == base["configuration"]["config_revision"]


def test_conflicts_and_invalid_rotation_never_modify_active_state(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as store:
        invalid = calibration_request(base)
        invalid["calibrations"][0]["calibration"]["rotation"][0][0] = 2
        with pytest.raises(ValueError):
            store.propose(invalid, "invalid")
        request = calibration_request(base)
        store.propose(request, "request-1")
        assert store.propose(request, "request-1")["job_id"] == "request-1"
        with pytest.raises(module.ControlConflict):
            store.propose({**request, "config_revision": "changed"}, "request-1")
        with pytest.raises(module.ControlConflict):
            store.propose(request, "request-2")


def mark_applied(state, base):
    cfg = state.state["active_registration"]
    for service in ["lidar-processing", "measurement-uplink", "edge-orchestrator"]:
        from pathlib import Path

        (Path(base["status_dir"]) / f"{service}.json").write_text(
            json.dumps(
                {
                    "service": service,
                    "site_id": cfg["site_id"],
                    "edge_id": cfg["edge_id"],
                    "config_revision": cfg["config_revision"],
                    "state": "HEALTHY",
                    "reported_at": utc_now(),
                    "clock_domain_id": "boot-test",
                }
            )
        )


async def test_applied_requires_all_fresh_service_revisions_and_version_conflicts_survive_restart(
    tmp_path,
):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        state.propose(calibration_request(base), "request-1")
        client = Delivery()
        client.acked = True
        await state.advance(client)
        assert state.job("request-1")["state"] == "APPLYING"
        mark_applied(state, base)
        assert state.job("request-1")["state"] == "APPLIED"
    with module.ControlState(base) as state:
        request = calibration_request(base)
        request.update(base_config_revision="cal-update-1", config_revision="cal-update-2")
        request["calibrations"][0]["calibration"]["translation_mm"][2] = 2000
        with pytest.raises(module.ControlConflict, match="CALIBRATION_VERSION_REUSED"):
            state.propose(request, "request-2")


async def test_unverified_request_stays_unverified_after_ack(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        request = calibration_request(base)
        request["calibrations"][0]["calibration"]["verification_state"] = "UNVERIFIED"
        state.propose(request, "unverified")
        client = Delivery()
        client.acked = True
        await state.advance(client)
        effective = module.effective_runtime(base)
        obs = transform_scan(scan(), effective["configuration"]["sensors"][0])
        assert obs["scan"]["points"][0]["position_mm"] is None
        assert obs["transform_state"] == "UNVERIFIED"


def test_single_writer_and_corrupted_or_missing_state_fail_closed(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base):
        with pytest.raises(OSError):
            module.ControlState(base)
    from pathlib import Path

    path = Path(base["configuration_state_file"])
    path.write_text("invalid json")
    with pytest.raises(ValueError):
        module.effective_runtime(base)
    path.unlink()
    with pytest.raises(OSError):
        module.effective_runtime(base)


@pytest.mark.parametrize("key", ["bad/key", "space key", "\uac00"])
def test_request_ids_are_safe_retrievable_path_identifiers(tmp_path, key):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        with pytest.raises(ValueError):
            state.propose(calibration_request(base), key)


def test_failed_atomic_write_does_not_accept_or_activate_request(tmp_path, monkeypatch):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        old = deepcopy(state.state)

        def failed_replace(*args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(module.os, "replace", failed_replace)
        with pytest.raises(OSError):
            state.propose(calibration_request(base), "request-1")
        assert state.state == old
        assert module.effective_runtime(base)["configuration"] == base["configuration"]


async def test_runtime_loader_uses_acked_overlay_and_preserves_source_revision(tmp_path):
    from pathlib import Path

    from ajin_edge.v2 import runtime

    module = control_module()
    base = base_runtime(tmp_path)
    base.update(
        drivers={"lidar-a": "unix:/sockets/a.sock", "lidar-b": "unix:/sockets/b.sock"},
        delivery_endpoint="unix:/sockets/delivery.sock",
        observation_endpoint="unix:/sockets/observations.sock",
        storage_dir=str(tmp_path / "outbox"),
        backend_origin="https://backend.example",
        token_file=str(tmp_path / "token"),
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(base))
    with module.ControlState(base) as state:
        state.propose(calibration_request(base), "request-1")
        client = Delivery()
        client.acked = True
        await state.advance(client)
        active = runtime.load_runtime(config_path)
        assert active["configuration"]["config_revision"] == "cal-update-1"
        assert active["driver_config_revision"] == base["configuration"]["config_revision"]
        assert Path(active["configuration_state_file"]).parent == Path(active["control_dir"])


@pytest.mark.parametrize("change", ["reflection", "nonfinite", "unknown", "duplicate", "too_far"])
def test_invalid_calibration_never_creates_pending_registration(tmp_path, change):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        request = calibration_request(base)
        cal = request["calibrations"][0]
        if change == "reflection":
            cal["calibration"]["rotation"][0][0] = -1
        elif change == "nonfinite":
            cal["calibration"]["translation_mm"][0] = float("nan")
        elif change == "unknown":
            cal["sensor_id"] = "unknown"
        elif change == "too_far":
            cal["calibration"]["translation_mm"][0] = 1e100
        else:
            request["calibrations"].append(deepcopy(cal))
        with pytest.raises(ValueError):
            state.propose(request, "bad-input")
        assert state.state["pending"] is None


async def test_rejected_job_history_cannot_evict_current_active_job(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        state.propose(calibration_request(base), "active-job")
        client = Delivery()
        client.acked = True
        await state.advance(client)
        for i in range(65):
            mark_applied(state, base)
            request = calibration_request(base)
            request.update(base_config_revision="cal-update-1", config_revision=f"rejected-{i}")
            state.propose(request, f"rejected-{i}")
            rejected = Delivery()
            rejected.state = "quarantine"
            await state.advance(rejected)
        assert len(state.state["requests"]) == 64
        assert state.job("active-job")["state"] == "APPLIED"


def test_control_state_rejects_out_of_band_source_and_configuration_changes(tmp_path):
    from pathlib import Path

    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base):
        pass
    changed = deepcopy(base)
    changed["driver_config_revision"] = "other-source"
    with pytest.raises(ValueError):
        module.effective_runtime(changed)
    path = Path(base["configuration_state_file"])
    state = json.loads(path.read_text())
    state["active_registration"]["sensors"][0]["angle_intervals_mdeg"][0]["end_exclusive"] = 90000
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        module.effective_runtime(base)


async def test_previous_boot_status_does_not_prove_current_application(tmp_path):
    from pathlib import Path

    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        state.propose(calibration_request(base), "request-1")
        client = Delivery()
        client.acked = True
        await state.advance(client)
        mark_applied(state, base)
        for path in Path(base["status_dir"]).glob("*.json"):
            if path.name == "calibration-control.json":
                continue
            data = json.loads(path.read_text())
            data["clock_domain_id"] = "previous-boot"
            path.write_text(json.dumps(data))
        assert state.job("request-1")["state"] == "APPLYING"


def test_restart_cleans_only_its_uncommitted_control_temporary_files(tmp_path):
    from pathlib import Path

    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base):
        pass
    root = Path(base["control_dir"])
    (root / ".calibration.aborted.tmp").write_text("uncommitted")
    (root / "other-service.tmp").write_text("untouched")
    with module.ControlState(base):
        assert not (root / ".calibration.aborted.tmp").exists()
        assert (root / "other-service.tmp").read_text() == "untouched"


def test_calibration_state_cannot_share_writable_telemetry_directory(tmp_path):
    module = control_module()
    base = base_runtime(tmp_path)
    base["control_dir"] = base["status_dir"]
    with pytest.raises(ValueError):
        module.ControlState(base)


@pytest.mark.parametrize(
    "name", [".calibration-control.lock", ".calibration.active.tmp", "active.txt"]
)
def test_control_state_filename_cannot_collide_with_lock_or_cleanup(tmp_path, name):
    from pathlib import Path

    base = base_runtime(tmp_path)
    base["configuration_state_file"] = str(Path(base["control_dir"]) / name)
    with pytest.raises(ValueError):
        control_module().ControlState(base)


async def test_missing_previously_initialized_state_cannot_reset_active_calibration(tmp_path):
    from pathlib import Path

    module = control_module()
    base = base_runtime(tmp_path)
    with module.ControlState(base) as state:
        state.propose(calibration_request(base), "request-1")
        delivery = Delivery()
        delivery.acked = True
        await state.advance(delivery)
    Path(base["configuration_state_file"]).unlink()
    with pytest.raises(ValueError, match="missing"):
        module.ControlState(base)
