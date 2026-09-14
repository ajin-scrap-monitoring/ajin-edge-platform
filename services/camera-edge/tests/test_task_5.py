from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from camera_edge import Backoff, LatestFrameSlot, Settings, WssPublisher
from camera_edge.main import _shutdown_capture_executor, run_edge_stream


def settings() -> Settings:
    return Settings.from_env(
        {
            "CAMERA_ID": "camera-5",
            "MEDIA_WSS_URL": "wss://media.example/ingest",
            "CAMERA_TOKEN": "test-bearer-token",
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "ws://media.example/ingest",
        "http://media.example",
        "media.example",
        "wss://",
        "wss:///ingest",
        "wss://?camera=1",
    ],
)
def test_settings_rejects_non_wss_media_url(url: str) -> None:
    with pytest.raises(ValueError, match="MEDIA_WSS_URL"):
        Settings.from_env(
            {
                "CAMERA_ID": "camera-5",
                "MEDIA_WSS_URL": url,
                "CAMERA_TOKEN": "token",
            }
        )


@pytest.mark.asyncio
async def test_latest_frame_slot_waits_for_generation_published_by_another_thread() -> None:
    slot = LatestFrameSlot()
    baseline = slot.publish(b"\xff\xd8old\xff\xd9")
    publisher = threading.Thread(target=lambda: slot.publish(b"\xff\xd8new\xff\xd9"), daemon=True)

    publisher.start()
    newer = await asyncio.to_thread(slot.wait_for_newer, baseline, 1.0)
    publisher.join(timeout=1.0)

    assert newer == (2, b"\xff\xd8new\xff\xd9")
    assert not publisher.is_alive()


@pytest.mark.asyncio
async def test_capture_blocking_in_worker_does_not_block_connection_or_async_publisher() -> None:
    capture = CaptureDuringSlowSend()
    connector = Connector()
    publisher = SlowStopPublisher()
    task = asyncio.create_task(
        run_edge_stream(
            settings(),
            slot=LatestFrameSlot(),
            capture=capture,
            publisher=publisher,
            connect=connector,
            sleep=asyncio.sleep,
        )
    )

    assert await wait_for_condition(lambda: connector.calls == 1)
    await asyncio.sleep(0)
    capture.ready.set()
    assert await wait_for_thread_event(publisher.send_started)
    capture_continued = await wait_for_thread_event(capture.second_capture_started)
    publisher.release.set()

    with pytest.raises(StopAsyncIteration):
        await task

    assert publisher.frames == [b"\xff\xd8first\xff\xd9"]
    assert capture_continued
    assert set(capture.thread_names) == {"camera-capture_0"}


@pytest.mark.asyncio
async def test_reconnection_waits_for_a_new_frame_instead_of_sending_the_old_slot_value() -> None:
    slot = LatestFrameSlot()
    slot.publish(b"\xff\xd8stale\xff\xd9")
    capture = EmptyCapture()
    connector = Connector()
    publisher = StopAfterOnePublisher()
    task = asyncio.create_task(
        run_edge_stream(
            settings(),
            slot=slot,
            capture=capture,
            publisher=publisher,
            connect=connector,
            sleep=asyncio.sleep,
        )
    )

    assert await wait_for_condition(lambda: connector.calls == 1)
    await asyncio.sleep(0)
    slot.publish(b"\xff\xd8fresh\xff\xd9")

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(task, timeout=0.25)

    assert publisher.frames == [b"\xff\xd8fresh\xff\xd9"]


@pytest.mark.asyncio
async def test_unauthorized_handshake_is_fatal_and_logs_without_credentials_or_frame_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = EmptyCapture()
    sleeps: list[float] = []
    raw_frame = b"\xff\xd8never-log-this\xff\xd9"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(Exception) as error:
            await run_edge_stream(
                settings(),
                slot=LatestFrameSlot(),
                capture=capture,
                publisher=WssPublisher(),
                connect=FailingConnector(FakeHandshakeError(401, raw_frame)),
                sleep=record_delay(sleeps),
            )

    assert type(error.value).__name__ == "FatalAuthenticationError"
    assert sleeps == []
    assert "test-bearer-token" not in caplog.text
    assert "never-log-this" not in caplog.text
    fatal = next(record for record in caplog.records if record.state == "fatal")
    assert fatal.component == "edge_wss"
    assert fatal.error_category == "authentication"


@pytest.mark.asyncio
async def test_backoff_grows_across_failed_connections_and_resets_only_after_a_send() -> None:
    capture = QueueCapture()
    connector = Connector()
    publisher = OutcomesPublisher([OSError("send-1"), OSError("send-2"), True, OSError("send-3")])
    sleeps: list[float] = []
    task = asyncio.create_task(
        run_edge_stream(
            settings(),
            slot=LatestFrameSlot(),
            capture=capture,
            publisher=publisher,
            connect=connector,
            backoff=Backoff(base_delay=1.0, max_delay=8.0),
            sleep=record_delay(sleeps),
        )
    )

    for expected_call in range(1, 4):
        assert await wait_for_condition(lambda expected=expected_call: connector.calls >= expected)
        capture.frames.put(b"\xff\xd8frame-" + str(expected_call).encode() + b"\xff\xd9")
        assert await wait_for_condition(lambda expected=expected_call: publisher.calls >= expected)

    capture.frames.put(b"\xff\xd8frame-4\xff\xd9")
    assert await wait_for_condition(lambda: publisher.calls == 4)
    assert await wait_for_condition(lambda: len(sleeps) == 3)
    assert sleeps == [1.0, 2.0, 1.0]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_capture_worker_failure_is_logged_restarted_and_unblocks_frame_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = FailOnceCapture()
    connector = Connector()
    publisher = StopAfterOnePublisher()
    sleeps: list[float] = []

    with caplog.at_level(logging.INFO):
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(
                run_edge_stream(
                    settings(),
                    slot=LatestFrameSlot(),
                    capture=capture,
                    publisher=publisher,
                    connect=connector,
                    backoff=Backoff(base_delay=0.01, max_delay=0.1),
                    capture_backoff=Backoff(base_delay=0.01, max_delay=0.1),
                    sleep=record_delay(sleeps),
                ),
                timeout=1.0,
            )

    assert capture.calls >= 2
    assert publisher.frames == [b"\xff\xd8recovered\xff\xd9"]
    assert sleeps == [0.01]
    failure = next(record for record in caplog.records if record.message == "capture_worker_failed")
    assert failure.component == "edge_capture"
    assert failure.camera_id == "camera-5"
    assert failure.service_version == "0.1.0"
    assert failure.state == "retrying"
    assert failure.error_category == "capture"
    assert failure.error_type == "RuntimeError"
    assert "camera offline" not in caplog.text


@pytest.mark.asyncio
async def test_capture_worker_restarts_while_wss_connection_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture = FailOnceCapture()
    connector = FailingForeverConnector()
    sleeps: list[float] = []
    task = asyncio.create_task(
        run_edge_stream(
            settings(),
            slot=LatestFrameSlot(),
            capture=capture,
            publisher=WssPublisher(),
            connect=connector,
            backoff=Backoff(base_delay=0.01, max_delay=0.1),
            capture_backoff=Backoff(base_delay=0.01, max_delay=0.1),
            sleep=record_delay_with_bounded_wait(sleeps),
        )
    )

    try:
        assert await asyncio.to_thread(capture.recovered.wait, 1.0)
        assert await wait_for_condition(lambda: connector.calls >= 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert capture.calls >= 2
    assert connector.calls >= 2
    assert 0.01 in sleeps
    failure = next(record for record in caplog.records if record.message == "capture_worker_failed")
    assert failure.component == "edge_capture"


@pytest.mark.asyncio
async def test_capture_executor_shutdown_drains_inflight_call_before_container_close() -> None:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-capture")
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def blocking_capture() -> None:
        started.set()
        release.wait(1.0)
        order.append("capture_finished")

    executor.submit(blocking_capture)
    assert await wait_for_thread_event(started)

    shutdown_task = asyncio.create_task(_shutdown_capture_executor(executor))
    try:
        await asyncio.sleep(0)
        assert not shutdown_task.done()
        release.set()
        await shutdown_task

        order.append("container_closed")
        assert order == ["capture_finished", "container_closed"]
    finally:
        release.set()
        if not shutdown_task.done():
            await shutdown_task


@pytest.mark.asyncio
async def test_cancelled_capture_executor_shutdown_drains_before_container_close() -> None:
    """A PyAV read has no safe cross-thread interrupt, so cancellation still drains it."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-capture")
    started = threading.Event()
    release_read = threading.Event()
    allow_finish = threading.Event()
    read_released = threading.Event()
    order: list[str] = []

    def blocking_capture() -> None:
        started.set()
        release_read.wait(1.0)
        read_released.set()
        allow_finish.wait(1.0)
        order.append("capture_finished")

    executor.submit(blocking_capture)
    assert await wait_for_thread_event(started)

    shutdown_task = asyncio.create_task(_shutdown_capture_executor(executor))
    try:
        await asyncio.sleep(0)
        assert not shutdown_task.done()

        shutdown_task.cancel()
        release_read.set()
        assert await wait_for_thread_event(read_released)
        await asyncio.sleep(0)

        # Container closure must remain impossible while the worker is finishing.
        assert not shutdown_task.done()
        allow_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

        order.append("container_closed")
        assert order == ["capture_finished", "container_closed"]
    finally:
        release_read.set()
        allow_finish.set()
        if not shutdown_task.done():
            with pytest.raises(asyncio.CancelledError):
                await shutdown_task


@pytest.mark.asyncio
async def test_wss_state_and_periodic_stats_logs_are_structured_and_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_settings = Settings.from_env(
        {
            "CAMERA_ID": "camera-5",
            "MEDIA_WSS_URL": "wss://media.example/ingest",
            "CAMERA_TOKEN": "test-bearer-token",
            "STATS_INTERVAL_SECONDS": "7",
        }
    )
    capture = QueueCapture()
    connector = Connector()
    publisher = HoldingPublisher()
    stats_wakeup = asyncio.Event()
    stats_delays: list[float] = []

    async def release_stats(delay: float) -> None:
        stats_delays.append(delay)
        await stats_wakeup.wait()
        stats_wakeup.clear()

    task = asyncio.create_task(
        run_edge_stream(
            runtime_settings,
            slot=LatestFrameSlot(),
            capture=capture,
            publisher=publisher,
            connect=connector,
            stats_sleep=release_stats,
        )
    )

    try:
        with caplog.at_level(logging.INFO):
            assert await wait_for_condition(lambda: connector.calls == 1)
            capture.frames.put(b"\xff\xd8observed\xff\xd9")
            assert await wait_for_thread_event(publisher.sent)
            publisher.release.set()
            assert await wait_for_condition(
                lambda: any(
                    getattr(record, "state", None) == "streaming" for record in caplog.records
                )
            )
            stats_wakeup.set()
            assert await wait_for_condition(
                lambda: any(record.message == "edge_stats" for record in caplog.records)
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    states = {
        record.state
        for record in caplog.records
        if getattr(record, "component", None) == "edge_wss"
    }
    assert {"connecting", "authenticated", "streaming"} <= states
    for record in caplog.records:
        if getattr(record, "component", None) in {"edge_wss", "edge_stats"}:
            assert record.camera_id == "camera-5"
            assert record.service_version == "0.1.0"
    stats = next(record for record in caplog.records if record.message == "edge_stats")
    assert stats.component == "edge_stats"
    assert stats.camera_state == "streaming"
    assert stats.capture_frames >= 1
    assert stats.sent_frames == 1
    assert stats.sent_bytes == len(b"\xff\xd8observed\xff\xd9")
    assert stats.stats_interval_seconds == 7
    assert stats_delays[0] == 7
    assert "test-bearer-token" not in caplog.text
    assert "media.example" not in caplog.text
    assert "observed" not in caplog.text


class QueueCapture:
    def __init__(self) -> None:
        self.frames: queue.Queue[bytes] = queue.Queue()

    def publish_next(self, slot: LatestFrameSlot) -> bool:
        try:
            frame = self.frames.get_nowait()
        except queue.Empty:
            return False
        slot.publish(frame)
        return True


class EmptyCapture:
    def publish_next(self, slot: LatestFrameSlot) -> bool:
        return False


class FailOnceCapture:
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = threading.Event()

    def publish_next(self, slot: LatestFrameSlot) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("camera offline")
        self.recovered.set()
        slot.publish(b"\xff\xd8recovered\xff\xd9")
        return True


class CaptureDuringSlowSend:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.second_capture_started = threading.Event()
        self._calls = 0
        self.thread_names: list[str] = []

    def publish_next(self, slot: LatestFrameSlot) -> bool:
        self.thread_names.append(threading.current_thread().name)
        if not self.ready.is_set():
            return False
        self._calls += 1
        if self._calls == 1:
            slot.publish(b"\xff\xd8first\xff\xd9")
            return True
        self.second_capture_started.set()
        return False


class RecordingWebSocket:
    async def send(self, payload: bytes) -> None:
        return None

    async def close(self) -> None:
        return None


class Connector:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, uri: str, **kwargs: object) -> RecordingWebSocket:
        self.calls += 1
        return RecordingWebSocket()


class FailingConnector:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self._called = False

    async def __call__(self, uri: str, **kwargs: object) -> RecordingWebSocket:
        if self._called:
            raise StopAsyncIteration
        self._called = True
        raise self._error


class FailingForeverConnector:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, uri: str, **kwargs: object) -> RecordingWebSocket:
        self.calls += 1
        raise OSError("media unavailable")


class FakeHandshakeError(Exception):
    def __init__(self, status_code: int, unsafe_payload: bytes) -> None:
        super().__init__(f"status={status_code} payload={unsafe_payload!r}")
        self.status_code = status_code


class StopAfterOnePublisher:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def publish_once(self, slot: LatestFrameSlot, websocket: RecordingWebSocket) -> bool:
        latest = slot.latest()
        assert latest is not None
        self.frames.append(latest[1])
        raise StopAsyncIteration


class SlowStopPublisher:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.send_started = threading.Event()
        self.release = threading.Event()

    async def publish_once(self, slot: LatestFrameSlot, websocket: RecordingWebSocket) -> bool:
        latest = slot.latest()
        assert latest is not None
        self.frames.append(latest[1])
        self.send_started.set()
        await asyncio.to_thread(self.release.wait, 1.0)
        raise StopAsyncIteration


class HoldingPublisher:
    def __init__(self) -> None:
        self.sent = threading.Event()
        self.release = threading.Event()

    async def publish_once(self, slot: LatestFrameSlot, websocket: RecordingWebSocket) -> bool:
        latest = slot.latest()
        assert latest is not None
        self.sent.set()
        await asyncio.to_thread(self.release.wait, 1.0)
        return True


class OutcomesPublisher:
    def __init__(self, outcomes: list[bool | Exception]) -> None:
        self._outcomes = iter(outcomes)
        self.calls = 0

    async def publish_once(self, slot: LatestFrameSlot, websocket: RecordingWebSocket) -> bool:
        self.calls += 1
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def no_delay(delay: float) -> None:
    return None


def record_delay(calls: list[float]) -> Callable[[float], Awaitable[None]]:
    async def _delay(delay: float) -> None:
        calls.append(delay)
        await asyncio.sleep(0)

    return _delay


def record_delay_with_bounded_wait(calls: list[float]) -> Callable[[float], Awaitable[None]]:
    async def _delay(delay: float) -> None:
        calls.append(delay)
        await asyncio.sleep(min(delay, 0.001))

    return _delay


async def wait_for_thread_event(event: threading.Event) -> bool:
    return await asyncio.to_thread(event.wait, 1.0)


async def wait_for_condition(condition: Callable[[], bool]) -> bool:
    for _ in range(100):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False
