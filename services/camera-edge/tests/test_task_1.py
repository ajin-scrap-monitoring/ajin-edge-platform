from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from camera_edge import (
    Backoff,
    LatestFrameSlot,
    PyAvMjpegCaptureAdapter,
    Settings,
    WssPublisher,
    is_jpeg,
)
from camera_edge.main import (
    connect_websocket,
    run_edge_stream,
)


def test_settings_from_env_requires_camera_id() -> None:
    with pytest.raises(ValueError, match="CAMERA_ID"):
        Settings.from_env(
            {
                "MEDIA_WSS_URL": "wss://media.example/ws",
                "CAMERA_TOKEN": "secret",
            }
        )


def test_settings_from_env_requires_media_wss_url() -> None:
    with pytest.raises(ValueError, match="MEDIA_WSS_URL"):
        Settings.from_env(
            {
                "CAMERA_ID": "cam-1",
                "CAMERA_TOKEN": "secret",
            }
        )


def test_settings_from_env_requires_camera_token() -> None:
    with pytest.raises(ValueError, match="CAMERA_TOKEN"):
        Settings.from_env(
            {
                "CAMERA_ID": "cam-1",
                "MEDIA_WSS_URL": "wss://media.example/ws",
            }
        )


def test_settings_from_env_applies_defaults() -> None:
    settings = Settings.from_env(
        {
            "CAMERA_ID": "cam-1",
            "MEDIA_WSS_URL": "wss://media.example/ws",
            "CAMERA_TOKEN": "secret",
        }
    )

    assert settings.camera_id == "cam-1"
    assert settings.media_wss_url == "wss://media.example/ws"
    assert settings.camera_token == "secret"
    assert settings.camera_device == "/dev/video0"
    assert settings.camera_width == 1920
    assert settings.camera_height == 1080
    assert settings.camera_fps == 30
    assert settings.camera_input_format == "mjpeg"
    assert settings.ws_send_timeout_seconds == 2
    assert settings.ws_ping_interval_seconds == 20
    assert settings.ws_ping_timeout_seconds == 20
    assert settings.ws_reconnect_max_seconds == 30
    assert settings.max_frame_bytes == 4_194_304
    assert settings.stats_interval_seconds == 60
    assert settings.log_format == "json"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CAMERA_DEVICE", ""),
        ("CAMERA_DEVICE", "   "),
        ("CAMERA_INPUT_FORMAT", ""),
        ("CAMERA_INPUT_FORMAT", "   "),
    ],
)
def test_settings_from_env_rejects_empty_string_fields(key: str, value: str) -> None:
    environ = {
        "CAMERA_ID": "cam-1",
        "MEDIA_WSS_URL": "wss://media.example/ws",
        "CAMERA_TOKEN": "secret",
        key: value,
    }

    with pytest.raises(ValueError, match=key):
        Settings.from_env(environ)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CAMERA_WIDTH", "0"),
        ("CAMERA_HEIGHT", "-1"),
        ("CAMERA_FPS", "0"),
        ("WS_SEND_TIMEOUT_SECONDS", "0"),
        ("WS_PING_INTERVAL_SECONDS", "-1"),
        ("WS_PING_TIMEOUT_SECONDS", "0"),
        ("WS_RECONNECT_MAX_SECONDS", "-1"),
        ("MAX_FRAME_BYTES", "0"),
        ("STATS_INTERVAL_SECONDS", "0"),
    ],
)
def test_settings_from_env_rejects_non_positive_numeric_fields(key: str, value: str) -> None:
    environ = {
        "CAMERA_ID": "cam-1",
        "MEDIA_WSS_URL": "wss://media.example/ws",
        "CAMERA_TOKEN": "secret",
        key: value,
    }

    with pytest.raises(ValueError, match=key):
        Settings.from_env(environ)


def test_latest_frame_slot_replaces_previous_frame_and_returns_generation() -> None:
    slot = LatestFrameSlot()

    first_generation = slot.publish(b"\xff\xd8first\xff\xd9")
    second_generation = slot.publish(b"\xff\xd8second\xff\xd9")

    assert first_generation == 1
    assert second_generation == 2
    assert slot.latest() == (2, b"\xff\xd8second\xff\xd9")


def test_latest_frame_slot_returns_defensive_copy() -> None:
    slot = LatestFrameSlot()
    slot.publish(b"\xff\xd8frame\xff\xd9")

    generation, frame = slot.latest()

    assert generation == 1
    assert frame == b"\xff\xd8frame\xff\xd9"
    assert frame is not slot.latest()[1]


def test_backoff_caps_and_resets() -> None:
    backoff = Backoff(base_delay=1.0, max_delay=3.0)

    assert backoff.next_delay() == 1.0
    assert backoff.next_delay() == 2.0
    assert backoff.next_delay() == 3.0
    assert backoff.next_delay() == 3.0

    backoff.reset()

    assert backoff.next_delay() == 1.0


def test_backoff_applies_deterministic_jitter() -> None:
    backoff = Backoff(base_delay=2.0, max_delay=30.0, jitter=lambda: 0.25)

    assert backoff.next_delay() == 2.5
    assert backoff.next_delay() == 4.5


def test_is_jpeg_accepts_only_non_empty_jpeg_bytes() -> None:
    assert is_jpeg(b"\xff\xd8payload\xff\xd9") is True
    assert is_jpeg(b"") is False
    assert is_jpeg(b"\xff\xd8payload") is False
    assert is_jpeg(b"payload\xff\xd9") is False
    assert is_jpeg(b"\xff\xd8\xff\xd9junk") is False


@pytest.mark.asyncio
async def test_wss_publisher_sends_latest_frame_as_binary_bytes() -> None:
    slot = LatestFrameSlot()
    websocket = RecordingWebSocket()
    publisher = WssPublisher()
    slot.publish(b"\xff\xd8frame\xff\xd9")

    sent = await publisher.publish_once(slot, websocket)

    assert sent is True
    assert websocket.sent_messages == [b"\xff\xd8frame\xff\xd9"]


@pytest.mark.asyncio
async def test_wss_publisher_returns_false_when_no_frame_exists() -> None:
    websocket = RecordingWebSocket()
    publisher = WssPublisher()

    sent = await publisher.publish_once(LatestFrameSlot(), websocket)

    assert sent is False
    assert websocket.sent_messages == []


def test_pyav_capture_adapter_publishes_only_valid_frames_within_size_limit() -> None:
    packets = [
        FakePacket(b"not-jpeg"),
        FakePacket(b"\xff\xd8valid\xff\xd9"),
        FakePacket(b"\xff\xd8ignored\xff\xd9"),
    ]
    slot = LatestFrameSlot()
    adapter = PyAvMjpegCaptureAdapter(
        container=FakeContainer(packets),
        stream_selector=0,
        max_frame_bytes=1024,
    )

    published = adapter.publish_next(slot)

    assert published is True
    assert slot.latest() == (1, b"\xff\xd8valid\xff\xd9")


def test_pyav_capture_adapter_skips_oversized_frames() -> None:
    oversized = b"\xff\xd8" + (b"x" * 32) + b"\xff\xd9"
    adapter = PyAvMjpegCaptureAdapter(
        container=FakeContainer([FakePacket(oversized)]),
        stream_selector=0,
        max_frame_bytes=8,
    )

    published = adapter.publish_next(LatestFrameSlot())

    assert published is False


@pytest.mark.asyncio
async def test_connect_websocket_uses_bearer_header_and_expected_options() -> None:
    settings = Settings.from_env(
        {
            "CAMERA_ID": "cam-1",
            "MEDIA_WSS_URL": "wss://media.example/ws",
            "CAMERA_TOKEN": "secret",
        }
    )
    recorder = ConnectRecorder()

    connection = await connect_websocket(settings, connect=recorder)

    assert connection is recorder.connection
    assert recorder.calls == [
        {
            "uri": "wss://media.example/ws",
            "additional_headers": {"Authorization": "Bearer secret"},
            "compression": None,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 4_194_304,
        }
    ]


@pytest.mark.asyncio
async def test_run_edge_stream_publishes_frames_from_capture_to_websocket() -> None:
    settings = Settings.from_env(
        {
            "CAMERA_ID": "cam-1",
            "MEDIA_WSS_URL": "wss://media.example/ws",
            "CAMERA_TOKEN": "secret",
        }
    )
    slot = LatestFrameSlot()
    capture = SequenceCaptureBoundary([b"\xff\xd8frame-1\xff\xd9", b"\xff\xd8frame-2\xff\xd9"])
    publisher = RecordingPublisher(stop_after=2)
    connector = ConnectRecorder()

    with pytest.raises(StopAsyncIteration):
        await run_edge_stream(
            settings,
            slot=slot,
            capture=capture,
            publisher=publisher,
            connect=connector,
            backoff=Backoff(base_delay=1.0, max_delay=30.0),
            sleep=fail_if_called,
        )

    assert capture.published_frames == [b"\xff\xd8frame-1\xff\xd9", b"\xff\xd8frame-2\xff\xd9"]
    assert publisher.frames == [b"\xff\xd8frame-1\xff\xd9", b"\xff\xd8frame-2\xff\xd9"]
    assert connector.calls[0]["compression"] is None


@pytest.mark.asyncio
async def test_run_edge_stream_retries_with_backoff_after_connection_failure() -> None:
    settings = Settings.from_env(
        {
            "CAMERA_ID": "cam-1",
            "MEDIA_WSS_URL": "wss://media.example/ws",
            "CAMERA_TOKEN": "secret",
        }
    )
    slot = LatestFrameSlot()
    capture = SequenceCaptureBoundary([b"\xff\xd8frame-1\xff\xd9"])
    publisher = RecordingPublisher(stop_after=1)
    connector = FlakyConnectRecorder(failures=1)
    sleep_calls: list[float] = []

    with pytest.raises(StopAsyncIteration):
        await run_edge_stream(
            settings,
            slot=slot,
            capture=capture,
            publisher=publisher,
            connect=connector,
            backoff=Backoff(base_delay=1.0, max_delay=30.0),
            sleep=record_sleep(sleep_calls),
        )

    assert sleep_calls == [1.0]
    assert connector.attempts == 2


@pytest.mark.asyncio
async def test_run_edge_stream_closes_websocket_when_publisher_stops() -> None:
    settings = Settings.from_env(
        {
            "CAMERA_ID": "cam-1",
            "MEDIA_WSS_URL": "wss://media.example/ws",
            "CAMERA_TOKEN": "secret",
        }
    )
    slot = LatestFrameSlot()
    capture = SequenceCaptureBoundary([b"\xff\xd8frame-1\xff\xd9"])
    connector = ConnectRecorder(connection=ClosableWebSocket())
    publisher = RecordingPublisher(stop_after=1)

    with pytest.raises(StopAsyncIteration):
        await run_edge_stream(
            settings,
            slot=slot,
            capture=capture,
            publisher=publisher,
            connect=connector,
            backoff=Backoff(base_delay=1.0, max_delay=30.0),
            sleep=fail_if_called,
        )

    assert connector.connection.close_calls == 1


@dataclass
class FakePacket:
    payload: bytes

    def __bytes__(self) -> bytes:
        return self.payload


class FakeContainer:
    def __init__(self, packets: list[FakePacket]) -> None:
        self._packets = packets
        self.demux_calls: list[int] = []

    def demux(self, stream_selector: int):
        self.demux_calls.append(stream_selector)
        return iter(self._packets)


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent_messages: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent_messages.append(payload)


class RecordingPublisher:
    def __init__(self, stop_after: int) -> None:
        self.frames: list[bytes] = []
        self._stop_after = stop_after

    async def publish_once(self, slot: LatestFrameSlot, websocket: RecordingWebSocket) -> bool:
        latest = slot.latest()
        if latest is None:
            return False
        _, frame = latest
        self.frames.append(frame)
        await websocket.send(frame)
        if len(self.frames) >= self._stop_after:
            raise StopAsyncIteration
        return True


class SequenceCaptureBoundary:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = list(frames)
        self.published_frames: list[bytes] = []

    def publish_next(self, slot: LatestFrameSlot) -> bool:
        if not self._frames:
            return False
        frame = self._frames.pop(0)
        slot.publish(frame)
        self.published_frames.append(frame)
        return True


class ConnectRecorder:
    def __init__(self, connection: RecordingWebSocket | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.connection = connection or RecordingWebSocket()

    async def __call__(self, uri: str, **kwargs: object) -> RecordingWebSocket:
        self.calls.append({"uri": uri, **kwargs})
        return self.connection


class FlakyConnectRecorder(ConnectRecorder):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures_remaining = failures
        self.attempts = 0

    async def __call__(self, uri: str, **kwargs: object) -> RecordingWebSocket:
        self.attempts += 1
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise OSError("connect failed")
        return await super().__call__(uri, **kwargs)


def fail_if_called(delay: float) -> asyncio.Future[None]:
    raise AssertionError(f"sleep should not be called, got {delay}")


def record_sleep(calls: list[float]):
    async def _sleep(delay: float) -> None:
        calls.append(delay)

    return _sleep


class ClosableWebSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
