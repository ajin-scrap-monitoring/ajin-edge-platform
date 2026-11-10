import os
from pathlib import Path

import pytest
from ajin_edge.v2.outbox import Outbox
from ajin_edge.v2.processing import envelope
from test_v2_contracts import message
from test_v2_processing import configuration


def actual_usage(root):
    total = 0
    for path in root.iterdir():
        if path.is_file():
            info = path.stat()
            total += max(Outbox._allocation(info.st_size), getattr(info, "st_blocks", 0) * 512)
    return total


def test_writing_and_acking_do_not_rescan_every_queued_file(tmp_path, monkeypatch):
    with Outbox(tmp_path) as store:
        for _ in range(40):
            store.enqueue(message())
        original = Path.iterdir
        original_scandir = os.scandir
        scans = []

        def tracked(path):
            if path == tmp_path:
                scans.append(path)
            return original(path)

        monkeypatch.setattr(Path, "iterdir", tracked)

        def tracked_scandir(path):
            if Path(path) == tmp_path:
                scans.append(path)
            return original_scandir(path)

        monkeypatch.setattr("ajin_edge.v2.outbox.os.scandir", tracked_scandir)
        for _ in range(8):
            body = message()
            store.enqueue(body)
            store.delivered(body["message_id"], "2026-11-10T00:00:00Z")
        assert len(scans) <= 1, "delivery work must not enumerate the whole spool per write"


def test_allocation_matches_filesystem_after_rewrite_ack_rotation_collection_and_restart(tmp_path):
    bodies = [message() for _ in range(36)]
    with Outbox(tmp_path) as store:
        store.protect("live", [bodies[0]["sensors"][0]["scan"]["scan_id"]], expires_at=10**12)
        for body in bodies:
            store.enqueue(body)
            assert store.used_bytes() == actual_usage(tmp_path)
        store.quarantine(bodies[-1]["message_id"], "SCHEMA_REJECTED")
        assert store.used_bytes() == actual_usage(tmp_path)
        for body in bodies[:-1]:
            store.delivered(body["message_id"], "2026-11-10T00:00:00Z")
            assert store.used_bytes() == actual_usage(tmp_path)
        store.release("live")
        assert store.used_bytes() == actual_usage(tmp_path)
    with Outbox(tmp_path) as store:
        assert store.used_bytes() == actual_usage(tmp_path)
        assert store.stats()["quarantined_messages"] == 1


def test_partial_temp_left_by_cleanup_error_counts_toward_budget(tmp_path, monkeypatch):
    body = message()
    with Outbox(tmp_path) as store:
        original_unlink = Path.unlink

        def fail_cleanup(path, *args, **kwargs):
            if path.suffix == ".tmp":
                raise OSError("temporary cleanup failed")
            return original_unlink(path, *args, **kwargs)

        def fail_sync(_fd):
            raise OSError("temporary file sync failed")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_cleanup)
            patch.setattr("ajin_edge.v2.outbox.os.fsync", fail_sync)
            with pytest.raises(OSError):
                store._atomic(store._path(body["message_id"]), {"payload": body})
        assert store.used_bytes() == actual_usage(tmp_path)


def test_pending_configuration_is_not_starved_by_old_and_new_evidence(tmp_path):
    cfg = configuration()
    cfg["coordinate_frame"].update(
        unit="mm",
        origin_description="Test",
        x_axis_description="X",
        y_axis_description="Y",
        z_axis_description="Z up",
        handedness="RIGHT_HANDED",
    )
    registration = {**cfg, **envelope(cfg, "edge_configuration")}
    with Outbox(tmp_path) as store:
        old = message()
        store.enqueue(old)
        store.enqueue(registration)
        for _ in range(8):
            store.enqueue(message())
        store.latest = False
        selected = store.next_ready(pin=True)
        assert selected["payload"]["message_id"] == registration["message_id"]
        next_row = store.next_ready(pin=True)
        assert next_row["payload"]["message_id"] == old["message_id"]


def test_status_audit_reconciles_external_file_allocation(tmp_path):
    with Outbox(tmp_path) as store:
        extra = tmp_path / "interrupted.tmp"
        extra.write_bytes(b"x" * 8193)
        assert store.stats()["used_bytes"] == actual_usage(tmp_path)
        extra.unlink()
        assert store.stats()["used_bytes"] == actual_usage(tmp_path)


def test_directory_sync_failure_after_replace_keeps_actual_allocation(tmp_path, monkeypatch):
    body = message()
    with Outbox(tmp_path) as store:
        store.enqueue(body)

        def fail_sync():
            raise OSError("directory sync failed")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_sync_directory", fail_sync)
            with pytest.raises(OSError):
                store.quarantine(body["message_id"], "SCHEMA_REJECTED")
        assert store.used_bytes() == actual_usage(tmp_path)
