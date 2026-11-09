import asyncio
import json
import logging
import threading

import pytest


def test_numeric_stage_windows_do_not_reset_when_status_is_read():
    from camera_edge.diagnostics import Diagnostics

    diagnostic = Diagnostics()
    diagnostic.observe("capture_native", 0.025)
    diagnostic.observe("capture_native", 0.8)
    first = diagnostic.snapshot()
    assert first["capture_native_max_seconds"] == 0.8
    assert first["capture_native_count"] == 2
    assert diagnostic.snapshot() == first
    diagnostic.reset_window()
    assert diagnostic.snapshot()["capture_native_count"] == 0


def test_formatter_emits_allowlisted_numeric_diagnostics_without_secrets():
    from camera_edge.diagnostics import DiagnosticFormatter, Diagnostics

    diagnostic = Diagnostics()
    diagnostic.observe("send_await", 0.25)
    record = logging.LogRecord("camera_edge.wss", logging.INFO, "", 0, "edge_stats", (), None)
    record.camera_token = "sensitive-test-token"
    record.endpoint = "wss://secret.example"
    record.camera_id = "test-camera"
    record.diagnostics = diagnostic.snapshot()
    value = json.loads(DiagnosticFormatter().format(record))
    assert value["event"] == "edge_stats"
    assert value["diagnostics"]["send_await_max_seconds"] == 0.25
    assert "sensitive" not in json.dumps(value)
    assert "secret.example" not in json.dumps(value)
    assert value["at"].endswith("Z")


@pytest.mark.asyncio
async def test_slow_status_storage_does_not_block_loop_and_cancellation_drains():
    from camera_edge.diagnostics import Diagnostics, StatusPublisher

    started = threading.Event()
    release = threading.Event()
    writes = []

    class Writer:
        def update(self, snapshot):
            started.set()
            release.wait(3)
            writes.append(snapshot)

    publisher = StatusPublisher(Writer(), Diagnostics())
    task = asyncio.create_task(publisher.write({"capture_frames": 1}))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert writes == [{"capture_frames": 1}]
    assert publisher.diagnostics.snapshot()["status_write_count"] == 1


@pytest.mark.asyncio
async def test_periodic_and_fatal_status_writes_are_serialized():
    from camera_edge.diagnostics import Diagnostics, StatusPublisher

    started = threading.Event()
    release = threading.Event()
    writes = []

    class Writer:
        def update(self, snapshot):
            if not writes:
                started.set()
                release.wait(3)
            writes.append(snapshot["state"])

    publisher = StatusPublisher(Writer(), Diagnostics())
    first = asyncio.create_task(publisher.write({"state": "streaming"}))
    assert await asyncio.to_thread(started.wait, 1)
    second = asyncio.create_task(publisher.write({"state": "fatal"}))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert writes == ["streaming", "fatal"]


def test_capture_worker_reports_native_finish_separately_from_loop_resume():
    from camera_edge.diagnostics import timed_capture

    result, duration, completed, error = timed_capture(lambda slot: True, object())
    assert result is True
    assert duration >= 0
    assert completed > 0
    assert error is None


def test_failed_capture_retains_native_duration_and_original_error(monkeypatch):
    from camera_edge import diagnostics

    times = iter((10.0, 11.5))
    error = OSError("unlogged device details")

    def fail(slot):
        raise error

    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: next(times))
    published, duration, completed, failure = diagnostics.timed_capture(fail, object())
    assert not published
    assert (duration, completed, failure) == (1.5, 11.5, error)


@pytest.mark.asyncio
async def test_stream_keeps_capturing_and_sending_while_status_disk_is_blocked():
    from test_task_5 import Connector, QueueCapture, settings

    from camera_edge.ingest import LatestFrameSlot, WssPublisher
    from camera_edge.main import run_edge_stream

    started = threading.Event()
    release = threading.Event()
    received = asyncio.Event()
    capture = QueueCapture()
    payload = b"\xff\xd8unchanged-jpeg\xff\xd9"

    class Writer:
        def update(self, snapshot):
            started.set()
            release.wait(3)

    class Socket:
        async def send(self, frame):
            assert frame == payload
            received.set()

        async def close(self):
            pass

    class Connected(Connector):
        async def __call__(self, *args, **kwargs):
            return Socket()

    task = asyncio.create_task(
        run_edge_stream(
            settings(),
            slot=LatestFrameSlot(),
            capture=capture,
            publisher=WssPublisher(),
            connect=Connected(),
            status_writer=Writer(),
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        capture.frames.put(payload)
        await asyncio.wait_for(received.wait(), 1)
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_status_disk_failure_is_reported_without_failing_stream(caplog):
    from camera_edge.diagnostics import Diagnostics, StatusPublisher

    class Writer:
        def update(self, snapshot):
            raise OSError("sensitive path")

    publisher = StatusPublisher(Writer(), Diagnostics())
    await publisher.write({})
    assert "edge_status_write_failed" in caplog.text
    assert "sensitive path" not in caplog.text
    assert publisher.diagnostics.snapshot()["status_write_count"] == 1


def test_delay_warning_is_bounded_per_stage(caplog):
    from camera_edge.diagnostics import Diagnostics

    diagnostic = Diagnostics()
    for _ in range(100):
        diagnostic.observe("capture_native", 1.0)
    assert len(caplog.records) == 1
    assert diagnostic.snapshot()["capture_native_count"] == 100


@pytest.mark.asyncio
async def test_repeated_stream_cancel_drains_capture_after_status_is_released():
    from test_task_5 import Connector, settings

    from camera_edge.ingest import LatestFrameSlot, WssPublisher
    from camera_edge.main import run_edge_stream

    capture_started = threading.Event()
    status_started = threading.Event()
    release_capture = threading.Event()
    release_status = threading.Event()
    status_completed = threading.Event()

    class Capture:
        def publish_next(self, slot):
            capture_started.set()
            release_capture.wait(3)
            return False

    class Writer:
        def update(self, snapshot):
            status_started.set()
            release_status.wait(3)
            status_completed.set()

    task = asyncio.create_task(
        run_edge_stream(
            settings(),
            slot=LatestFrameSlot(),
            capture=Capture(),
            publisher=WssPublisher(),
            connect=Connector(),
            status_writer=Writer(),
        )
    )
    try:
        assert await asyncio.to_thread(capture_started.wait, 1)
        assert await asyncio.to_thread(status_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release_status.set()
        assert await asyncio.to_thread(status_completed.wait, 1)
        # Drain ready callbacks; native capture still owns the camera.
        for _ in range(20):
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        release_capture.set()
        release_status.set()
        with pytest.raises(asyncio.CancelledError):
            await task
