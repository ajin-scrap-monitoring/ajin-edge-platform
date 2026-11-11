import asyncio
import threading

import grpc
import pytest
from ajin_edge.v2.ipc import DeliveryStub, serve_delivery
from ajin_edge.v2.outbox import Outbox
from test_v2_contracts import message


async def test_stats_rpc_does_not_queue_behind_slow_storage(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    with Outbox(tmp_path) as store:
        body = message()
        await store.call("enqueue", body)
        original = store.collect

        def slow_collect():
            entered.set()
            release.wait(5)
            return original()

        monkeypatch.setattr(store, "collect", slow_collect)
        server, port = await serve_delivery(
            store, "127.0.0.1:0", site_id=body["site_id"], edge_id=body["edge_id"]
        )
        blocked = asyncio.create_task(store.call("collect"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stats = await asyncio.wait_for(DeliveryStub(channel).stats(), 0.5)
                assert stats["pending_messages"] == 1
                assert stats["lost_messages"] == 0
        finally:
            release.set()
            await blocked
            await server.stop(0)


def test_stats_snapshot_never_exposes_mutable_or_stalled_storage(tmp_path, monkeypatch):
    with Outbox(tmp_path) as store:
        # A completed snapshot remains valid while idle, but not during a hung write.
        snapshot = store.realtime_stats()
        snapshot["unavailable_evidence_scan_ids"].append("foreign")
        assert store.realtime_stats()["unavailable_evidence_scan_ids"] == []
        monkeypatch.setattr(store, "_operation_started", 100.0)
        monkeypatch.setattr("ajin_edge.v2.outbox.time.monotonic", lambda: 103.001)
        with pytest.raises(RuntimeError, match="storage observation"):
            store.realtime_stats()


async def test_completed_loss_updates_publish_and_storage_failure_is_not_healthy(
    tmp_path, monkeypatch
):
    with Outbox(tmp_path) as store:
        body = message()
        await store.call("enqueue", body)
        assert store.realtime_stats()["pending_messages"] == 1

        def failed_collect():
            raise OSError("disk failure")

        monkeypatch.setattr(store, "collect", failed_collect)
        with pytest.raises(OSError):
            await store.call("collect")
        with pytest.raises(RuntimeError):
            store.realtime_stats()
        await store.call("inspect", body["message_id"])
        assert store.realtime_stats()["storage_observation_epoch"] == 1


def test_completed_slow_write_remains_visible_between_status_polls(tmp_path, monkeypatch):
    with Outbox(tmp_path) as store:
        clock = [100.0]
        monkeypatch.setattr("ajin_edge.v2.outbox.time.monotonic", lambda: clock[0])
        original = store.collect

        def slow_collect():
            clock[0] += 3.1
            return original()

        monkeypatch.setattr(store, "collect", slow_collect)
        store._timed_call("collect", clock[0], (), {})
        assert store.realtime_stats()["storage_observation_epoch"] == 1
        store._timed_call("inspect", clock[0], ("unknown",), {})
        assert store.realtime_stats()["storage_observation_epoch"] == 1


async def test_status_persistence_is_bounded_and_does_not_hold_evaluation(tmp_path, monkeypatch):
    from ajin_edge.v2.runtime import LatestStatusWriter, LocalStatus
    from test_v2_processing import configuration

    entered, release = threading.Event(), threading.Event()
    saved = []

    def slow_disk(snapshot):
        entered.set()
        release.wait(5)
        saved.append(snapshot)

    status = LocalStatus({"configuration": configuration(), "status_dir": str(tmp_path)}, "test")
    monkeypatch.setattr(status, "_persist", slow_disk)
    writer = LatestStatusWriter(status)
    task = asyncio.create_task(writer.run())
    try:
        writer.offer(tick=0)
        assert await asyncio.to_thread(entered.wait, 1)
        for tick in range(1, 101):
            writer.offer(tick=tick)
        assert writer.pending["tick"] == 100
        assert not saved
        release.set()
        for _ in range(100):
            if len(saved) == 2:
                break
            await asyncio.sleep(0.01)
        assert [s["tick"] for s in saved] == [0, 100]
        assert saved[0]["reported_at"] <= saved[1]["reported_at"]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_latest_writer_drains_inflight_before_generation_exit(
    tmp_path, monkeypatch
):
    from ajin_edge.v2.runtime import LatestStatusWriter, LocalStatus
    from test_v2_processing import configuration

    entered, release = threading.Event(), threading.Event()
    saved = []

    def slow_disk(snapshot):
        entered.set()
        release.wait(5)
        saved.append(snapshot["tick"])

    status = LocalStatus({"configuration": configuration(), "status_dir": str(tmp_path)}, "test")
    monkeypatch.setattr(status, "_persist", slow_disk)
    writer = LatestStatusWriter(status)
    task = asyncio.create_task(writer.run())
    try:
        writer.offer(tick=1)
        assert await asyncio.to_thread(entered.wait, 1)
        writer.offer(tick=2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert saved == [1]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
