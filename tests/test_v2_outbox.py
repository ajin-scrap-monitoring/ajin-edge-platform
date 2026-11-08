from copy import deepcopy

import pytest
from ajin_edge.v2.outbox import CapacityError, ConflictError, Outbox
from test_v2_contracts import message


def test_restart_duplicate_conflict_and_original_quarantine(tmp_path):
    body = message()
    with Outbox(tmp_path) as store:
        assert store.enqueue(body) is False
        assert store.enqueue(body) is True
        changed = deepcopy(body)
        changed["generated_at"] = "2026-09-20T02:00:00Z"
        with pytest.raises(ConflictError):
            store.enqueue(changed)
        store.quarantine(body["message_id"], "SCHEMA_REJECTED")
    with Outbox(tmp_path) as store:
        record = store.get(body["message_id"])
        assert record["payload"] == body
        assert record["reason"] == "SCHEMA_REJECTED"
        assert record["state"] == "quarantine"
        assert store.next_ready() is None
        assert store.stats()["quarantined_messages"] == 1


def test_latest_oldest_alternate(tmp_path):
    with Outbox(tmp_path) as store:
        bodies = [message() for _ in range(4)]
        for body in bodies:
            store.enqueue(body)
        first = store.next_ready()
        store.delivered(first["payload"]["message_id"], "2026-09-20T01:00:00Z")
        second = store.next_ready()
        assert first["payload"]["message_id"] == bodies[-1]["message_id"]
        assert second["payload"]["message_id"] == bodies[0]["message_id"]


def test_actual_file_budget_eviction_and_persistent_loss(tmp_path):
    limit = 96 * 1024
    with Outbox(tmp_path, limit_bytes=limit, reserve_bytes=16384) as store:
        for _ in range(60):
            store.enqueue(message())
            assert store.used_bytes() <= limit
        assert store.stats()["lost_messages"] > 0
        count = store.stats()["lost_messages"]
    with Outbox(tmp_path, limit_bytes=limit, reserve_bytes=16384) as store:
        assert store.stats()["lost_messages"] == count


def test_active_reference_protects_acknowledged_scan_until_release(tmp_path):
    with Outbox(tmp_path) as store:
        body = message()
        store.enqueue(body)
        scan_id = body["sensors"][0]["scan"]["scan_id"]
        store.protect("active-test", [scan_id], expires_at=100)
        store.delivered(body["message_id"], "2026-09-20T01:00:00Z", now=1)
        assert store.get(body["message_id"])["state"] == "acked"
        store.release("active-test", now=1)
        assert store.get(body["message_id"]) is None


def test_protected_only_capacity_fails_without_false_enqueue(tmp_path):
    with Outbox(tmp_path, limit_bytes=65536, reserve_bytes=16384) as store:
        first = message()
        store.enqueue(first)
        store.protect("active-test", [first["sensors"][0]["scan"]["scan_id"]], expires_at=10**12)
        for _ in range(20):
            body = message()
            # All generated fixtures reference this same scan, so all are protected.
            try:
                store.enqueue(body)
            except CapacityError:
                assert store.get(body["message_id"]) is None
                break
        else:
            pytest.fail("storage did not enforce physical budget")
        assert store.used_bytes() <= 65536
        assert store.stats()["lost_messages"] > 0


def test_v1_database_directory_is_refused(tmp_path):
    (tmp_path / "outbox.db").write_bytes(b"existing v1 data")
    with pytest.raises(ValueError):
        Outbox(tmp_path)
    assert (tmp_path / "outbox.db").read_bytes() == b"existing v1 data"


def test_expired_leases_release_capacity_across_restart(tmp_path):
    with Outbox(tmp_path) as store:
        for i in range(16):
            store.protect(f"old-{i}", ["scan-test"], expires_at=0)
        store.collect()
    with Outbox(tmp_path) as store:
        store.protect("new-owner", ["scan-test"], expires_at=10**12)
        assert len(store.meta["leases"]) == 1


def test_external_status_budget_is_reserved_and_reported(tmp_path):
    used = [4096]
    with Outbox(
        tmp_path,
        limit_bytes=98304,
        reserve_bytes=16384,
        external_reserve_bytes=32768,
        external_usage=lambda: used[0],
    ) as store:
        for _ in range(20):
            store.enqueue(message())
        spool_before = store.used_bytes() - used[0]
        used[0] = 32768
        assert store.used_bytes() == spool_before + 32768
        assert store.stats()["limit_bytes"] == 98304
        assert store.used_bytes() <= 98304


def test_ack_of_protected_scan_succeeds_without_space_for_another_body(tmp_path):
    with Outbox(tmp_path, limit_bytes=65536, reserve_bytes=16384) as store:
        first = message()
        store.enqueue(first)
        store.protect("live-judgment", [first["sensors"][0]["scan"]["scan_id"]], expires_at=10**12)
        for _ in range(30):
            try:
                store.enqueue(message())
            except CapacityError:
                break
        else:
            pytest.fail("test did not fill protected storage")
        losses = store.stats()["lost_messages"]
        store.delivered(first["message_id"], "2026-09-20T01:00:00Z")
        assert store.get(first["message_id"])["state"] == "acked"
        assert store.stats()["lost_messages"] == losses
    with Outbox(tmp_path, limit_bytes=65536, reserve_bytes=16384) as store:
        assert store.inspect(first["message_id"])["remote_acked"] is True
        assert store.get(first["message_id"])["state"] == "acked"


def test_protected_acks_remain_durable_after_receipt_rotation(tmp_path):
    bodies = [message() for _ in range(40)]
    with Outbox(tmp_path) as store:
        store.protect(
            "live-judgment", [bodies[0]["sensors"][0]["scan"]["scan_id"]], expires_at=10**12
        )
        for body in bodies:
            store.enqueue(body)
            store.delivered(body["message_id"], "2026-09-20T01:00:00Z")
        assert len(store.meta["receipts"]) == 32
    with Outbox(tmp_path) as store:
        assert store.next_ready() is None
        for body in bodies:
            assert store.inspect(body["message_id"])["remote_acked"] is True
            assert store.get(body["message_id"])["payload"] == body
        store.release("live-judgment")
        assert not store.index


def test_failed_ack_persistence_does_not_publish_a_false_receipt(tmp_path, monkeypatch):
    body = message()
    with Outbox(tmp_path) as store:
        store.enqueue(body)

        def no_space():
            raise CapacityError("diagnostic write unavailable")

        monkeypatch.setattr(store, "_save_meta", no_space)
        with pytest.raises(CapacityError):
            store.delivered(body["message_id"], "2026-09-20T01:00:00Z")
        assert not store.inspect(body["message_id"])["remote_acked"]
        assert store.get(body["message_id"])["state"] == "pending"
    with Outbox(tmp_path) as store:
        assert not store.inspect(body["message_id"])["remote_acked"]


def test_ack_rotation_at_full_protected_capacity_remains_durable(tmp_path):
    bodies = []
    with Outbox(tmp_path, limit_bytes=262144, reserve_bytes=16384) as store:
        first = message()
        store.protect("live", [first["sensors"][0]["scan"]["scan_id"]], expires_at=10**12)
        for _ in range(100):
            body = message()
            try:
                store.enqueue(body)
                bodies.append(body)
            except CapacityError:
                break
        assert len(bodies) > 32
        for body in bodies:
            store.delivered(body["message_id"], "2026-09-20T01:00:00Z")
    with Outbox(tmp_path, limit_bytes=262144, reserve_bytes=16384) as store:
        assert store.next_ready() is None
        assert all(store.inspect(b["message_id"])["remote_acked"] for b in bodies)
        store.release("live")
        assert not store.index
