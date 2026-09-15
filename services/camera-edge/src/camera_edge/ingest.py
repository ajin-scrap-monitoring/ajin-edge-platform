from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Condition
from typing import Protocol, TypeVar
from urllib.parse import urlsplit


class WebSocketLike(Protocol):
    async def send(self, payload: bytes) -> None: ...


class PacketLike(Protocol):
    def __bytes__(self) -> bytes: ...


StreamSelector = TypeVar("StreamSelector")


class PacketContainerLike(Protocol[StreamSelector]):
    def demux(self, stream_selector: StreamSelector): ...


@dataclass(frozen=True)
class Settings:
    camera_id: str
    media_wss_url: str
    camera_token: str
    camera_device: str = "/dev/video0"
    camera_width: int = 1920
    camera_height: int = 1080
    camera_fps: int = 30
    camera_input_format: str = "mjpeg"
    ws_send_timeout_seconds: int = 2
    ws_ping_interval_seconds: int = 20
    ws_ping_timeout_seconds: int = 20
    ws_reconnect_max_seconds: int = 30
    max_frame_bytes: int = 4_194_304
    stats_interval_seconds: int = 60
    log_format: str = "json"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        camera_id = _require_non_empty(environ, "CAMERA_ID")
        media_wss_url = _require_non_empty(environ, "MEDIA_WSS_URL")
        try:
            parsed_media_url = urlsplit(media_wss_url)
            has_hostname = bool(parsed_media_url.hostname)
        except ValueError as error:
            raise ValueError("MEDIA_WSS_URL must be a valid wss:// URL") from error
        if parsed_media_url.scheme != "wss" or not has_hostname:
            raise ValueError("MEDIA_WSS_URL must be a valid wss:// URL")
        camera_token = _require_non_empty(environ, "CAMERA_TOKEN")

        return cls(
            camera_id=camera_id,
            media_wss_url=media_wss_url,
            camera_token=camera_token,
            camera_device=_optional_non_empty(environ, "CAMERA_DEVICE", "/dev/video0"),
            camera_width=_positive_int(environ, "CAMERA_WIDTH", 1920),
            camera_height=_positive_int(environ, "CAMERA_HEIGHT", 1080),
            camera_fps=_positive_int(environ, "CAMERA_FPS", 30),
            camera_input_format=_optional_non_empty(environ, "CAMERA_INPUT_FORMAT", "mjpeg"),
            ws_send_timeout_seconds=_positive_int(environ, "WS_SEND_TIMEOUT_SECONDS", 2),
            ws_ping_interval_seconds=_positive_int(environ, "WS_PING_INTERVAL_SECONDS", 20),
            ws_ping_timeout_seconds=_positive_int(environ, "WS_PING_TIMEOUT_SECONDS", 20),
            ws_reconnect_max_seconds=_positive_int(environ, "WS_RECONNECT_MAX_SECONDS", 30),
            max_frame_bytes=_positive_int(environ, "MAX_FRAME_BYTES", 4_194_304),
            stats_interval_seconds=_positive_int(environ, "STATS_INTERVAL_SECONDS", 60),
            log_format=environ.get("LOG_FORMAT", "json"),
        )


class LatestFrameSlot:
    def __init__(self) -> None:
        self._condition = Condition()
        self._generation = 0
        self._frame: bytes | None = None

    def publish(self, frame: bytes) -> int:
        frame_copy = memoryview(frame).tobytes()
        with self._condition:
            self._generation += 1
            self._frame = frame_copy
            self._condition.notify_all()
            return self._generation

    def latest(self) -> tuple[int, bytes] | None:
        with self._condition:
            if self._frame is None:
                return None
            return self._generation, memoryview(self._frame).tobytes()

    def generation(self) -> int:
        with self._condition:
            return self._generation

    def wait_for_newer(
        self, generation: int, timeout: float | None = None
    ) -> tuple[int, bytes] | None:
        with self._condition:
            has_newer_frame = self._condition.wait_for(
                lambda: self._generation > generation, timeout
            )
            if not has_newer_frame or self._frame is None:
                return None
            return self._generation, memoryview(self._frame).tobytes()


@dataclass
class Backoff:
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: Callable[[], float] | None = None

    def __post_init__(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.base_delay * (2**self._attempt), self.max_delay)
        self._attempt += 1
        if self.jitter is None:
            return delay
        return min(delay + (self.base_delay * self.jitter()), self.max_delay)

    def reset(self) -> None:
        self._attempt = 0


def is_jpeg(data: bytes) -> bool:
    return bool(data) and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")


class WssPublisher:
    async def publish_once(self, frame: bytes | None, websocket: WebSocketLike) -> bool:
        """Send the immutable frame selected by the stream loop, without rereading capture."""
        if frame is None:
            return False
        await websocket.send(frame)
        return True


@dataclass
class PyAvMjpegCaptureAdapter:
    container: PacketContainerLike[StreamSelector]
    stream_selector: StreamSelector
    max_frame_bytes: int

    def publish_next(self, slot: LatestFrameSlot) -> bool:
        for packet in self.container.demux(self.stream_selector):
            frame = bytes(packet)
            if len(frame) > self.max_frame_bytes:
                continue
            if not is_jpeg(frame):
                continue
            slot.publish(frame)
            return True
        return False


def _require_non_empty(environ: Mapping[str, str], key: str) -> str:
    value = environ.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} must be non-empty")
    return value


def _optional_non_empty(environ: Mapping[str, str], key: str, default: str) -> str:
    if key not in environ:
        return default
    value = environ[key].strip()
    if not value:
        raise ValueError(f"{key} must be non-empty")
    return value


def _positive_int(environ: Mapping[str, str], key: str, default: int) -> int:
    value = int(environ.get(key, str(default)))
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value
