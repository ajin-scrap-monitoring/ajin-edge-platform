from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .ingest import Backoff, LatestFrameSlot, PyAvMjpegCaptureAdapter, Settings, WssPublisher
from .status import CameraStatusWriter


class CaptureBoundary(Protocol):
    def publish_next(self, slot: LatestFrameSlot) -> bool: ...


class PublisherBoundary(Protocol):
    async def publish_once(self, slot: LatestFrameSlot, websocket: Any) -> bool: ...


ConnectCallable = Callable[..., Awaitable[Any]]
SleepCallable = Callable[[float], Awaitable[None]]


LOGGER = logging.getLogger("camera_edge.wss")
SERVICE_VERSION = "0.1.0"
FRAME_WAIT_TIMEOUT_SECONDS = 0.1
EMPTY_CAPTURE_RETRY_DELAY_SECONDS = 0.05


class FatalAuthenticationError(RuntimeError):
    """A WSS handshake was rejected because the camera credential is invalid."""


@dataclass
class StreamStats:
    started_at: float = field(default_factory=time.monotonic)
    capture_frames: int = 0
    sent_frames: int = 0
    sent_bytes: int = 0
    capture_restarts: int = 0
    network_retries: int = 0
    total_frame_bytes: int = 0
    max_frame_bytes: int = 0
    last_sent_at: float | None = None
    camera_state: str = "starting"
    websocket_state: str = "disconnected"

    def record_capture(self, frame_size: int) -> None:
        self.capture_frames += 1
        self.total_frame_bytes += frame_size
        self.max_frame_bytes = max(self.max_frame_bytes, frame_size)

    def record_send(self, frame_size: int) -> None:
        self.sent_frames += 1
        self.sent_bytes += frame_size
        self.last_sent_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        return {
            "capture_fps": self.capture_frames / elapsed,
            "sent_fps": self.sent_frames / elapsed,
            "capture_frames": self.capture_frames,
            "sent_frames": self.sent_frames,
            "sent_bytes": self.sent_bytes,
            "average_frame_bytes": (
                self.total_frame_bytes / self.capture_frames if self.capture_frames else 0
            ),
            "max_frame_bytes": self.max_frame_bytes,
            "camera_state": self.camera_state,
            "websocket_state": self.websocket_state,
            "capture_restarts": self.capture_restarts,
            "network_retries": self.network_retries,
            "last_send_age_seconds": (
                None
                if self.last_sent_at is None
                else max(time.monotonic() - self.last_sent_at, 0.0)
            ),
        }


def create_capture_from_settings(
    settings: Settings,
    *,
    av_open: Callable[..., Any] | None = None,
) -> tuple[CaptureBoundary, Any]:
    if av_open is None:
        from av import open as av_open  # type: ignore[import-not-found]

    container = av_open(
        settings.camera_device,
        format="v4l2",
        options={
            "video_size": f"{settings.camera_width}x{settings.camera_height}",
            "framerate": str(settings.camera_fps),
            "input_format": settings.camera_input_format,
        },
    )
    stream_selector = container.streams.video[0]
    return (
        PyAvMjpegCaptureAdapter(
            container=container,
            stream_selector=stream_selector,
            max_frame_bytes=settings.max_frame_bytes,
        ),
        container,
    )


async def connect_websocket(
    settings: Settings,
    *,
    connect: ConnectCallable | None = None,
) -> Any:
    if connect is None:
        from websockets.asyncio.client import connect as connect  # type: ignore[import-not-found]

    return await connect(
        settings.media_wss_url,
        additional_headers={"Authorization": f"Bearer {settings.camera_token}"},
        compression=None,
        ping_interval=settings.ws_ping_interval_seconds,
        ping_timeout=settings.ws_ping_timeout_seconds,
        max_size=settings.max_frame_bytes,
    )


async def run_edge_stream(
    settings: Settings,
    *,
    slot: LatestFrameSlot,
    capture: CaptureBoundary,
    publisher: PublisherBoundary,
    connect: ConnectCallable | None = None,
    backoff: Backoff | None = None,
    capture_backoff: Backoff | None = None,
    sleep: SleepCallable = asyncio.sleep,
    stats_sleep: SleepCallable = asyncio.sleep,
    status_writer: CameraStatusWriter | None = None,
) -> None:
    reconnect_backoff = backoff or Backoff(
        max_delay=settings.ws_reconnect_max_seconds,
        jitter=random.random,
    )
    capture_retry_backoff = capture_backoff or Backoff(
        max_delay=settings.ws_reconnect_max_seconds,
        jitter=random.random,
    )
    stats = StreamStats()
    capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-capture")
    capture_task = asyncio.create_task(
        _capture_supervisor(
            capture,
            slot,
            capture_executor,
            capture_retry_backoff,
            sleep,
            stats,
            settings.camera_id,
        )
    )
    stats_task = asyncio.create_task(_periodic_stats(settings, stats, stats_sleep))
    status_task = (
        asyncio.create_task(_periodic_status(stats, status_writer)) if status_writer else None
    )

    try:
        while True:
            websocket = None
            try:
                stats.websocket_state = "connecting"
                _log_wss_state(settings.camera_id, "connecting", "none")
                websocket = await connect_websocket(settings, connect=connect)
                stats.websocket_state = "authenticated"
                _log_wss_state(settings.camera_id, "authenticated", "none")
                generation = slot.generation()
                streaming_logged = False
                while True:
                    newer = await asyncio.to_thread(
                        slot.wait_for_newer,
                        generation,
                        FRAME_WAIT_TIMEOUT_SECONDS,
                    )
                    if newer is None:
                        continue
                    generation, frame = newer
                    sent = await asyncio.wait_for(
                        publisher.publish_once(slot, websocket),
                        timeout=settings.ws_send_timeout_seconds,
                    )
                    if sent:
                        stats.record_send(len(frame))
                        stats.camera_state = "streaming"
                        if not streaming_logged:
                            _log_wss_state(settings.camera_id, "streaming", "none")
                            streaming_logged = True
                        reconnect_backoff.reset()
            except StopAsyncIteration:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if _is_fatal_authentication_error(error):
                    stats.websocket_state = "fatal"
                    _write_status(stats, status_writer)
                    _log_wss_state(settings.camera_id, "fatal", "authentication")
                    raise FatalAuthenticationError("WSS authentication failed") from error
                stats.network_retries += 1
                stats.websocket_state = "retrying"
                delay = reconnect_backoff.next_delay()
                _log_wss_state(settings.camera_id, "retrying", "transient")
                await sleep(delay)
            finally:
                stats.websocket_state = "disconnected"
                if websocket is not None:
                    await _close_maybe_awaitable(websocket)
    finally:
        capture_task.cancel()
        stats_task.cancel()
        tasks = [capture_task, stats_task]
        if status_task is not None:
            status_task.cancel()
            tasks.append(status_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await _shutdown_capture_executor(capture_executor)


async def async_main(
    *,
    environ: Mapping[str, str] | None = None,
    connect: ConnectCallable | None = None,
    av_open: Callable[..., Any] | None = None,
    slot: LatestFrameSlot | None = None,
    publisher: PublisherBoundary | None = None,
    backoff: Backoff | None = None,
    sleep: SleepCallable = asyncio.sleep,
) -> None:
    runtime_env = dict(os.environ if environ is None else environ)
    token_file = runtime_env.get("CAMERA_TOKEN_FILE")
    if token_file:
        token_path = Path(token_file)
        if token_path.stat().st_size > 4096:
            raise ValueError("camera credential file too large")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("invalid camera credential")
        runtime_env["CAMERA_TOKEN"] = token
    settings = Settings.from_env(runtime_env)
    status_dir = runtime_env.get("EDGE_STATUS_DIR")
    status_writer = (
        CameraStatusWriter(
            status_dir,
            runtime_env["EDGE_ID"],
            runtime_env["CONFIG_REVISION"],
            settings.camera_id,
            site_id=runtime_env.get("SITE_ID"),
            deployment_revision=runtime_env.get("DEPLOYMENT_REVISION"),
        )
        if status_dir
        else None
    )
    runtime_slot = slot or LatestFrameSlot()
    runtime_publisher = publisher or WssPublisher()
    capture, container = create_capture_from_settings(settings, av_open=av_open)
    try:
        await run_edge_stream(
            settings,
            slot=runtime_slot,
            capture=capture,
            publisher=runtime_publisher,
            connect=connect,
            backoff=backoff,
            sleep=sleep,
            status_writer=status_writer,
        )
    finally:
        # run_edge_stream drains the dedicated capture executor before returning.
        await _close_maybe_awaitable(container)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()


async def _close_maybe_awaitable(target: Any) -> None:
    close = getattr(target, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _shutdown_capture_executor(executor: ThreadPoolExecutor) -> None:
    """Drain capture work before its owner closes the PyAV container.

    PyAV's synchronous device read does not expose a safe cross-thread interrupt
    through this adapter. A graceful shutdown may therefore wait for the native
    read to return; cancellation must not let the caller close the container
    concurrently with that read.
    """
    shutdown_task = asyncio.create_task(
        asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
    )
    cancelled = False
    while not shutdown_task.done():
        try:
            await asyncio.shield(shutdown_task)
        except asyncio.CancelledError:
            cancelled = True
    await shutdown_task
    if cancelled:
        raise asyncio.CancelledError


async def _capture_supervisor(
    capture: CaptureBoundary,
    slot: LatestFrameSlot,
    executor: ThreadPoolExecutor,
    retry_backoff: Backoff,
    sleep: SleepCallable,
    stats: StreamStats,
    camera_id: str,
) -> None:
    while True:
        try:
            await _capture_in_worker(capture, slot, executor, stats)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            stats.capture_restarts += 1
            stats.camera_state = "degraded"
            _log_capture_failure(camera_id, error)
            await sleep(retry_backoff.next_delay())
            stats.camera_state = "starting"


async def _capture_in_worker(
    capture: CaptureBoundary,
    slot: LatestFrameSlot,
    executor: ThreadPoolExecutor,
    stats: StreamStats | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        published = await loop.run_in_executor(executor, capture.publish_next, slot)
        if published:
            if stats is not None:
                latest = slot.latest()
                if latest is not None:
                    stats.record_capture(len(latest[1]))
        else:
            await asyncio.sleep(EMPTY_CAPTURE_RETRY_DELAY_SECONDS)


async def _periodic_stats(
    settings: Settings,
    stats: StreamStats,
    sleep: SleepCallable,
) -> None:
    while True:
        await sleep(settings.stats_interval_seconds)
        _log_stats(settings.stats_interval_seconds, stats, camera_id=settings.camera_id)


def _write_status(stats: StreamStats, writer: CameraStatusWriter | None) -> None:
    if writer is not None:
        try:
            writer.update(stats.snapshot())
        except OSError:
            # Control-plane disk failure must not interrupt the binary JPEG stream.
            LOGGER.warning("edge_status_write_failed")


async def _periodic_status(stats: StreamStats, writer: CameraStatusWriter) -> None:
    while True:
        _write_status(stats, writer)
        await asyncio.sleep(5)


def _is_fatal_authentication_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    return status_code in {401, 403}


def _log_wss_state(camera_id: str, state: str, error_category: str) -> None:
    level = {
        "fatal": logging.ERROR,
        "retrying": logging.WARNING,
    }.get(state, logging.INFO)
    LOGGER.log(
        level,
        "wss_state",
        extra={
            "component": "edge_wss",
            "camera_id": camera_id,
            "service_version": SERVICE_VERSION,
            "state": state,
            "error_category": error_category,
        },
    )


def _log_capture_failure(camera_id: str, error: Exception) -> None:
    LOGGER.warning(
        "capture_worker_failed",
        extra={
            "component": "edge_capture",
            "camera_id": camera_id,
            "service_version": SERVICE_VERSION,
            "state": "retrying",
            "error_category": "capture",
            "error_type": type(error).__name__,
        },
    )


def _log_stats(stats_interval_seconds: int, stats: StreamStats, *, camera_id: str) -> None:
    LOGGER.info(
        "edge_stats",
        extra={
            "component": "edge_stats",
            "camera_id": camera_id,
            "service_version": SERVICE_VERSION,
            "stats_interval_seconds": stats_interval_seconds,
            **stats.snapshot(),
        },
    )
