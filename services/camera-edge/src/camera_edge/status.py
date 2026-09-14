"""Optional edge control-plane integration; no dependency on LiDAR services."""

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class CameraStatusWriter:
    def __init__(
        self,
        directory,
        edge_id,
        config_revision,
        camera_id,
        *,
        site_id=None,
        deployment_revision=None,
    ):
        for value in (edge_id, config_revision, camera_id, site_id, deployment_revision):
            if value is None:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
                raise ValueError("invalid edge status identity")
        self.path = Path(directory) / "camera-edge.json"
        self.identity = {
            "schema_version": "1.0",
            "service": "camera-edge",
            "service_version": "0.1.0",
            "edge_id": edge_id,
            "config_revision": config_revision,
            "camera_id": camera_id,
            "instance_id": str(uuid4()),
            **{
                key: value
                for key, value in (
                    ("site_id", site_id),
                    ("deployment_revision", deployment_revision),
                )
                if value is not None
            },
        }

    def update(self, snapshot):
        websocket, age = snapshot["websocket_state"], snapshot["last_send_age_seconds"]
        if websocket == "fatal":
            state, reasons = "FATAL", ["CAMERA_AUTHENTICATION_FAILED"]
        elif (
            websocket == "authenticated"
            and snapshot["camera_state"] == "streaming"
            and age is not None
            and age <= 5
        ):
            state, reasons = "HEALTHY", []
        elif websocket in {"connecting", "retrying"}:
            state, reasons = "RETRYING", ["CAMERA_NETWORK_RETRY"]
        elif snapshot["camera_state"] == "starting":
            state, reasons = "STARTING", []
        else:
            state, reasons = "DEGRADED", ["CAMERA_FRAME_STALE"]
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        value = {
            **snapshot,
            **self.identity,
            "state": state,
            "reason_codes": reasons,
            "reported_at": now,
            "last_progress_at": now,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".camera-edge.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
