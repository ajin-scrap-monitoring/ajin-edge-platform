import os
from pathlib import Path

import pytest
from ajin_edge.v2.outbox import CapacityError, Outbox
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


def test_reference_protection_does_not_copy_large_sets_for_each_record(tmp_path, monkeypatch):
    # Catch quadratic reference-set copying while retaining ACKed evidence.
    class CountedSet(set):
        copies = 0

        def __or__(self, other):
            self.copies += 1
            return super().__or__(other)

    with Outbox(tmp_path) as store:
        bodies = [message() for _ in range(80)]
        for body in bodies:
            store.enqueue(body)
        scan = bodies[0]["sensors"][0]["scan"]["scan_id"]
        store.protect("judgment", [scan], expires_at=10**12)
        for body in bodies:
            store.delivered(body["message_id"], "2026-11-10T00:00:00Z")
        original = store._protected
        active = CountedSet()

        def protected(now=None):
            a, r = original(now)
            active.clear()
            active.update(a)
            return active, r | {f"reference-{i}" for i in range(30000)}

        monkeypatch.setattr(store, "_protected", protected)
        store.collect()
        assert len(store.index) == 80  # Still protected, not deleted for speed.
        assert active.copies <= 2, "reference sets copied once per ACKed record"
        active.copies = 0
        store.limit = store.used_bytes() + store.reserve
        with pytest.raises(CapacityError):
            store._make_room(4096)
        assert len(store.index) == 80
        assert active.copies <= 4, "capacity search copied references per candidate"


def test_frequent_status_reads_reuse_accounting_but_eventually_reconcile(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("ajin_edge.v2.outbox.time.monotonic", lambda: now[0])
    with Outbox(tmp_path) as store:
        body = message()
        store.enqueue(body)
        original = os.scandir
        audits = []

        def scandir(path):
            if Path(path) == tmp_path:
                audits.append(now[0])
            return original(path)

        monkeypatch.setattr("ajin_edge.v2.outbox.os.scandir", scandir)
        for _ in range(20):
            assert store.stats(audit=False)["pending_messages"] == 1
        assert len(audits) <= 1
        (tmp_path / "orphan.tmp").write_bytes(b"x" * 8193)
        now[0] += 61
        assert store.stats(audit=False)["used_bytes"] == actual_usage(tmp_path)
        assert len(audits) >= 1


def test_repeated_collection_does_not_walk_pending_backlog(tmp_path):
    class CountedIndex(dict):
        walks = 0

        def items(self):
            self.walks += 1
            return super().items()

        def values(self):
            self.walks += 1
            return super().values()

    with Outbox(tmp_path) as store:
        for _ in range(40):
            store.enqueue(message())
        store.index = CountedIndex(store.index)
        for _ in range(30):
            store.collect()
        assert len(store.index) == 40
        assert store.index.walks <= 2, "collection repeatedly walks all pending messages"


def test_reference_cache_tracks_judgment_ack_and_restart(tmp_path):
    from test_v2_judgment_delivery import judgment_bundle

    bundle = judgment_bundle()
    evidence, assessment = bundle[:-1], bundle[-1]
    with Outbox(tmp_path) as store:
        store.collect()  # Warm an empty reference view before inserting a judgment.
        store.enqueue_bundle(bundle)
        for body in evidence:
            store.delivered(body["message_id"], "2026-11-10T00:00:00Z")
        store.collect()
        assert all(store.get(body["message_id"]) is not None for body in evidence)
    with Outbox(tmp_path) as store:
        store.collect()
        assert all(store.get(body["message_id"]) is not None for body in evidence)
        store.delivered(assessment["message_id"], "2026-11-10T00:00:01Z")
        assert not store.index


def test_selection_does_not_walk_pending_backlog_and_retries_pins(tmp_path):
    class CountedIndex(dict):
        walks = 0

        def items(self):
            self.walks += 1
            return super().items()

    with Outbox(tmp_path) as store:
        bodies = [message() for _ in range(80)]
        for body in bodies:
            store.enqueue(body)
        store.index = CountedIndex(store.index)
        selected = []
        for _ in range(20):
            row = store.next_ready(pin=True)
            selected.append(row["payload"]["message_id"])
        assert len(set(selected)) == 20
        assert selected[:2] == [bodies[-1]["message_id"], bodies[0]["message_id"]]
        assert store.index.walks <= 2, "selection scans all pending bodies for every slot"
        for mid in selected:
            store.unpin(mid)
        assert store.next_ready()["payload"]["message_id"] == bodies[-1]["message_id"]


async def test_storage_metrics_separate_execution_from_queue_wait(tmp_path):
    with Outbox(tmp_path) as store:
        await store.call("enqueue", message())
        metrics = (await store.call("stats", audit=False))["storage_metrics"]
        assert metrics["enqueue"]["calls"] == 1
        assert metrics["enqueue"]["run_ms"] >= 0
        assert metrics["enqueue"]["queue_ms"] >= 0


def test_retry_heap_compaction_retains_pinned_and_pending_records(tmp_path):
    with Outbox(tmp_path) as store:
        bodies = [message() for _ in range(40)]
        for body in bodies:
            store.enqueue(body)
        held = [store.next_ready(pin=True)["payload"]["message_id"] for _ in range(8)]
        for _ in range(500):
            row = store.next_ready(pin=True)
            assert row["payload"]["message_id"] not in held
            store.unpin(row["payload"]["message_id"])
        assert max(len(store._ready_old), len(store._ready_new)) <= 2 * len(store.index) + 64
        for mid in held:
            store.unpin(mid)
        selected = {store.next_ready(pin=True)["payload"]["message_id"] for _ in range(40)}
        assert selected == {b["message_id"] for b in bodies}
        assert store.next_ready(pin=True) is None
    with Outbox(tmp_path) as store:
        selected = {store.next_ready(pin=True)["payload"]["message_id"] for _ in range(40)}
        assert selected == {b["message_id"] for b in bodies}


def test_batch_gives_live_periodic_and_state_a_slot_during_evidence_backlog(tmp_path):
    from copy import deepcopy
    from uuid import uuid4

    from test_v2_judgment_delivery import judgment_bundle

    bundle = judgment_bundle()
    periodic = message()
    with Outbox(tmp_path) as store:
        store.enqueue_bundle(bundle)
        store.enqueue(periodic)
        for _ in range(20):
            body = deepcopy(bundle[0])
            body["message_id"] = str(uuid4())
            store.enqueue(body)
        selected = store.next_ready_many(2)
        assert {r["payload"]["message_id"] for r in selected} == {
            periodic["message_id"],
            bundle[-1]["message_id"],
        }


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


def test_ack_rename_stat_failure_keeps_body_reachable_and_retryable(tmp_path, monkeypatch):
    bodies = [message() for _ in range(33)]
    with Outbox(tmp_path) as store:
        store.protect("live", [bodies[0]["sensors"][0]["scan"]["scan_id"]], expires_at=10**12)
        for body in bodies:
            store.enqueue(body)
        for body in bodies[:32]:
            store.delivered(body["message_id"], "2026-11-10T00:00:00Z")
        stat = Path.stat
        failed = False

        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path.name.endswith(".acked.record") and not failed:
                failed = True
                raise OSError("temporary allocation inspection failure")
            return stat(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", fail_once)
            with pytest.raises(OSError):
                store.delivered(bodies[-1]["message_id"], "2026-11-10T00:00:00Z")
        assert store.index[bodies[0]["message_id"]]["path"].exists(), (
            "the renamed ACK body must stay reachable after allocation inspection fails"
        )
        assert store.get(bodies[0]["message_id"])["payload"] == bodies[0]
        store.delivered(bodies[-1]["message_id"], "2026-11-10T00:00:00Z")
        assert store.used_bytes() == actual_usage(tmp_path)
    with Outbox(tmp_path) as store:
        assert all(store.inspect(body["message_id"])["remote_acked"] for body in bodies)
