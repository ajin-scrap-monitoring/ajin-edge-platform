"""Atomic per-process snapshots; liveness is not sensor data freshness."""

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import __version__
from .config import identifier

STATES = {"STARTING", "HEALTHY", "DEGRADED", "RETRYING", "FATAL", "UNKNOWN"}


def utc_now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StatusWriter:
    def __init__(
        self,
        directory,
        service,
        edge_id,
        config_revision,
        *,
        site_id=None,
        deployment_revision=None,
    ):
        self.service = identifier(service)
        self.edge_id = identifier(edge_id)
        self.config_revision = identifier(config_revision)
        self.metadata = {
            key: identifier(value)
            for key, value in (
                ("site_id", site_id or os.environ.get("SITE_ID")),
                (
                    "deployment_revision",
                    deployment_revision or os.environ.get("DEPLOYMENT_REVISION"),
                ),
            )
            if value is not None
        }
        self.instance_id = str(uuid4())
        self.path = Path(directory) / f"{service}.json"

    def write(self, state, reason_codes=(), **extra):
        if state not in STATES:
            raise ValueError("unknown service state")
        now = utc_now()
        # Extra telemetry cannot overwrite the identity or freshness contract.
        value = {
            **extra,
            **self.metadata,
            "schema_version": "1.0",
            "service": self.service,
            "service_version": __version__,
            "instance_id": self.instance_id,
            "edge_id": self.edge_id,
            "config_revision": self.config_revision,
            "reported_at": now,
            "last_progress_at": now,
            "state": state,
            "reason_codes": list(reason_codes),
        }
        atomic_json(self.path, value)


def read_status(path, *, now=None, max_age=30):
    path = Path(path)
    unknown = {"service": path.stem, "state": "UNKNOWN", "reason_codes": ["STATUS_STALE"]}
    try:
        if path.stat().st_size > 65536:
            return unknown
        data = json.loads(path.read_text(encoding="utf-8"))
        json.dumps(data, allow_nan=False)
        if (
            data["schema_version"] != "1.0"
            or data["service"] != path.stem
            or data["state"] not in STATES
        ):
            return unknown
        now = now or datetime.now(UTC)
        stale = False
        for key in ("reported_at", "last_progress_at"):
            stamp = datetime.fromisoformat(data[key].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not -1 <= (now - stamp).total_seconds() <= max_age:
                stale = True
        for key in ("instance_id", "edge_id", "config_revision"):
            identifier(data[key])
        for key in ("site_id", "deployment_revision", "camera_id", "service_version"):
            if key in data:
                identifier(data[key])
        reasons = data["reason_codes"]
        if not isinstance(reasons, list) or len(reasons) > 64:
            return unknown
        for reason in reasons:
            identifier(reason)
        # A terminal startup diagnostic remains actionable after the process exits.
        # This does not claim liveness; a successful startup atomically replaces it.
        if stale:
            if data["state"] != "FATAL" or not any(
                reason.startswith("CONFIG_") for reason in reasons
            ):
                return unknown
            for key in ("site_id", "deployment_revision", "camera_id"):
                identifier(data[key])
            data["diagnostic_stale"] = True
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return unknown


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    state = read_status(args.path)["state"]
    # Retrying is alive but unready. Docker health does not restart containers.
    raise SystemExit(0 if state in {"HEALTHY", "DEGRADED", "RETRYING"} else 1)
