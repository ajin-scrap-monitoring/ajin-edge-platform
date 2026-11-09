"""Bounded, payload-free timing at the camera producer boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("camera_edge.wss")
STAGES = (
    "capture_native",
    "capture_resume",
    "capture_interval",
    "send_await",
    "send_interval",
    "status_write",
    "loop_lag",
)
FIELDS = (
    "component",
    "camera_id",
    "service_version",
    "state",
    "error_category",
    "error_type",
    "stats_interval_seconds",
    "capture_fps",
    "sent_fps",
    "capture_frames",
    "sent_frames",
    "sent_bytes",
    "average_frame_bytes",
    "max_frame_bytes",
    "camera_state",
    "websocket_state",
    "capture_restarts",
    "network_retries",
    "last_send_age_seconds",
    "selected_generation_skips",
    "diagnostics",
    "stage",
    "duration_seconds",
)


class DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.msg,
        }
        for name in FIELDS:
            if hasattr(record, name):
                value[name] = getattr(record, name)
        # Never format exception text, arbitrary extras, transport URLs or payloads.
        return json.dumps(value, allow_nan=False)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DiagnosticFormatter())
    LOGGER.handlers[:] = [handler]
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


class Diagnostics:
    def __init__(self) -> None:
        self._last_warning: dict[str, float] = {}
        self.reset_window()

    def reset_window(self) -> None:
        self._values = {stage: [0, 0.0, 0.0] for stage in STAGES}

    def observe(self, stage: str, duration: float) -> None:
        values = self._values[stage]
        duration = max(0.0, duration)
        values[0] += 1
        values[1] += duration
        values[2] = max(values[2], duration)
        now = time.monotonic()
        if duration >= 0.5 and now - self._last_warning.get(stage, -float("inf")) >= 30:
            self._last_warning[stage] = now
            LOGGER.warning("edge_stage_delay", extra={"stage": stage, "duration_seconds": duration})

    def snapshot(self) -> dict[str, int | float]:
        return {
            f"{stage}_{suffix}": value
            for stage, values in self._values.items()
            for suffix, value in zip(
                ("count", "total_seconds", "max_seconds"), values, strict=False
            )
        }


def timed_capture(publish_next: Any, slot: Any) -> tuple[bool, float, float, Exception | None]:
    start = time.monotonic()
    error = None
    try:
        result = publish_next(slot)
    except Exception as failure:
        result = False
        error = failure
    completed = time.monotonic()
    return result, completed - start, completed, error


async def observe_loop(diagnostics: Diagnostics) -> None:
    while True:
        expected = time.monotonic() + 0.1
        await asyncio.sleep(0.1)
        diagnostics.observe("loop_lag", time.monotonic() - expected)


class StatusPublisher:
    """One status writer; shutdown cannot orphan a filesystem write."""

    def __init__(self, writer: Any, diagnostics: Diagnostics) -> None:
        self.writer = writer
        self.diagnostics = diagnostics
        self._lock = asyncio.Lock()

    async def write(self, snapshot: dict[str, Any]) -> None:
        async with self._lock:
            frozen = dict(snapshot)
            start = time.monotonic()
            task = asyncio.create_task(asyncio.to_thread(self.writer.update, frozen))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except OSError:
                    break
            try:
                await task
            except OSError:
                LOGGER.warning("edge_status_write_failed")
            finally:
                self.diagnostics.observe("status_write", time.monotonic() - start)
            if cancelled:
                raise asyncio.CancelledError
