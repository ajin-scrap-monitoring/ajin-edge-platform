import asyncio
import threading

from ajin_edge.v2.runtime import LocalStatus
from test_v2_processing import configuration


async def test_slow_status_disk_write_does_not_block_scan_loop_and_owns_snapshot(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    saved = []

    def slow_disk(path, payload):
        entered.set()
        release.wait(2)
        saved.append(payload)

    monkeypatch.setattr("ajin_edge.v2.runtime.atomic_json", slow_disk)
    status = LocalStatus({"configuration": configuration(), "status_dir": str(tmp_path)}, "test")
    telemetry = {"value": 1}
    writer = asyncio.create_task(status.write_async(metrics=telemetry))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not writer.done()
        # The event loop must still receive new input while durability is blocked.
        status.advanced()
        telemetry["value"] = 2
        status.reason = "NEW_ERROR"
        release.set()
        await writer
        assert saved[0]["metrics"] == {"value": 1}
        assert saved[0]["reason_codes"] == []
        assert status.reason == "NEW_ERROR"
    finally:
        release.set()
        await writer


async def test_cancelled_status_writer_finishes_before_next_generation(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    completed = []

    def slow_disk(path, payload):
        entered.set()
        release.wait(2)
        completed.append(payload["instance_id"])

    monkeypatch.setattr("ajin_edge.v2.runtime.atomic_json", slow_disk)
    status = LocalStatus({"configuration": configuration(), "status_dir": str(tmp_path)}, "test")
    writer = asyncio.create_task(status.write_async())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        writer.cancel()
        await asyncio.sleep(0.01)
        assert not writer.done()
        release.set()
        result = await asyncio.gather(writer, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert completed == [status.instance_id]
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
