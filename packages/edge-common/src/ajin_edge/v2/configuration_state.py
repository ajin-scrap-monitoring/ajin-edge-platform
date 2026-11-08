"""Bounded, single-writer calibration transactions and ACK-gated active configuration."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .auxiliary_storage import auxiliary_bytes
from .calibration_schema import CALIBRATION_REQUEST_SCHEMA
from .contracts import validate_message
from .processing import canonical, identifier, utc_now

MAX_STATE_BYTES = 128 * 1024
STATUS_BUDGET = 1024 * 1024
REQUEST_VALIDATOR = Draft202012Validator(CALIBRATION_REQUEST_SCHEMA)


class ControlError(ValueError):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


class ControlConflict(ControlError):
    def __init__(self, code="CONFIGURATION_CONFLICT"):
        super().__init__(code, 409)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def registration_digest(registration):
    return digest(
        {k: v for k, v in registration.items() if k not in ("message_id", "generated_at")}
    )


def baseline_digest(base):
    return digest(
        {
            "registration": registration_digest(base["configuration"]),
            "source_revision": base["driver_config_revision"],
        }
    )


def immutable_registration(registration):
    result = {
        k: v
        for k, v in registration.items()
        if k not in ("message_id", "generated_at", "config_revision")
    }
    result["sensors"] = [
        {k: v for k, v in s.items() if k != "calibration"} for s in registration["sensors"]
    ]
    return result


def read_state(base):
    path = control_path(base)
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError("control state exceeds budget")
    state = json.loads(path.read_bytes())
    canonical(state)
    if (
        state["version"] != 1
        or state["baseline_digest"] != baseline_digest(base)
        or state["source_revision"] != base["driver_config_revision"]
    ):
        raise ValueError("control deployment baseline mismatch")
    validate_message(state["active_registration"])
    activation = state["activation"]
    if activation is not None:
        identifier(activation["clock_domain_id"])
        if not re.fullmatch(r"[0-9]{1,20}", activation["monotonic_ns"]):
            raise ValueError("invalid activation clock")
    active = state["active_registration"]
    if immutable_registration(active) != immutable_registration(base["configuration"]) or state[
        "revision_digests"
    ].get(active["config_revision"]) != registration_digest(active):
        raise ValueError("invalid active calibration configuration")
    pending = state["pending"]
    if pending is not None:
        candidate = pending["registration"]
        validate_message(candidate)
        if (
            immutable_registration(candidate) != immutable_registration(base["configuration"])
            or state["revision_digests"].get(candidate["config_revision"])
            != registration_digest(candidate)
            or pending["base_revision"] != active["config_revision"]
        ):
            raise ValueError("invalid pending calibration configuration")
    return state


def control_path(base):
    path = Path(base["configuration_state_file"])
    control = Path(base["control_dir"])
    if (
        not control.is_absolute()
        or not path.is_absolute()
        or path.parent.resolve() != control.resolve()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", path.name)
        or path.resolve().parent != control.resolve()
    ):
        raise ValueError("control state must use a dedicated absolute control directory")
    for key in ("status_dir", "storage_dir"):
        if key not in base:
            continue
        other = Path(base[key]).resolve()
        own = control.resolve()
        if own == other or own in other.parents or other in own.parents:
            raise ValueError("control, status and outbox directories must be separate siblings")
    return path


def effective_runtime(base):
    result = deepcopy(base)
    if "configuration_state_file" in base:
        state = read_state(base)
        result["configuration"] = state["active_registration"]
        result["configuration_activation"] = state["activation"]
    return result


class ControlState:
    def __init__(self, base):
        self.base = deepcopy(base)
        identifier(base["driver_config_revision"])
        validate_message(base["configuration"])
        self.path = control_path(base)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = (self.path.parent / ".calibration-control.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.lock.seek(0)
                if not self.lock.read(1):
                    self.lock.write(b"0")
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for temporary in self.path.parent.glob(".calibration.*.tmp"):
                temporary.unlink()
            marker = self.path.parent / ".calibration-initialized"
            if marker.exists() and not self.path.exists():
                raise ValueError("previously initialized calibration state is missing")
            if self.path.exists():
                self.state = read_state(base)
                if not marker.exists():
                    self._mark_initialized(marker, base)
            else:
                # Fail closed after any interrupted first installation as well as
                # later state loss; never silently reset version/request history.
                self._mark_initialized(marker, base)
                cfg = deepcopy(base["configuration"])
                self.state = {}
                self._commit(
                    {
                        "version": 1,
                        "baseline_digest": baseline_digest(base),
                        "source_revision": base["driver_config_revision"],
                        "active_registration": cfg,
                        "activation": None,
                        "active_job": None,
                        "pending": None,
                        "requests": {},
                        "request_digests": {},
                        "revision_digests": {cfg["config_revision"]: registration_digest(cfg)},
                        "calibration_digests": self._calibration_hashes(cfg),
                    }
                )
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _mark_initialized(marker, base):
        with marker.open("xb") as file:
            file.write(baseline_digest(base).encode("ascii"))
            file.flush()
            os.fsync(file.fileno())
        if os.name != "nt":
            descriptor = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def close(self):
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _calibration_hashes(cfg):
        return {
            digest([s["sensor_id"], s["calibration"]["version"]]): digest(s["calibration"])
            for s in cfg["sensors"]
            if s["calibration"] is not None
        }

    def _commit(self, state):
        data = canonical(state)
        allocation = ((len(data) + 4095) // 4096) * 4096
        used = auxiliary_bytes(self.base)
        if (
            len(data) > MAX_STATE_BYTES
            or used + allocation + 65536 > STATUS_BUDGET
            or shutil.disk_usage(self.path.parent).free < allocation + 65536
        ):
            raise ControlError("CONTROL_STORAGE_FULL", 507)
        fd, temporary = tempfile.mkstemp(
            prefix=".calibration.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self.state = state
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def snapshot(self):
        cfg = self.state["active_registration"]
        return {
            "site_id": cfg["site_id"],
            "edge_id": cfg["edge_id"],
            "config_revision": cfg["config_revision"],
            "coordinate_frame_id": cfg["coordinate_frame"]["frame_id"],
            "calibrations": [
                {"sensor_id": s["sensor_id"], "calibration": deepcopy(s["calibration"])}
                for s in cfg["sensors"]
            ],
            "pending_job_id": self.state["pending"]["job_id"] if self.state["pending"] else None,
        }

    def job(self, job_id):
        if job_id not in self.state["requests"]:
            raise ControlError("JOB_NOT_FOUND", 404)
        job = {"job_id": job_id, **deepcopy(self.state["requests"][job_id])}
        if job["state"] == "APPLYING":
            if self.state["active_registration"]["config_revision"] != job["config_revision"]:
                job["state"] = "SUPERSEDED"
            else:
                applied = []
                for service in ("lidar-processing", "edge-orchestrator", "measurement-uplink"):
                    path = Path(self.base["status_dir"]) / f"{service}.json"
                    try:
                        if path.stat().st_size > 65536:
                            continue
                        status = json.loads(path.read_bytes())
                        age = (
                            datetime.now(UTC) - datetime.fromisoformat(status["reported_at"])
                        ).total_seconds()
                        cfg = self.state["active_registration"]
                        boot = Path(self.base["clock_domain_file"]).read_text().strip()
                        if (
                            status["service"] == service
                            and status["config_revision"] == job["config_revision"]
                            and status["site_id"] == cfg["site_id"]
                            and status["edge_id"] == cfg["edge_id"]
                            and status["state"] in ("HEALTHY", "DEGRADED")
                            and status.get("clock_domain_id") == boot
                            and 0 <= age <= 5
                        ):
                            applied.append(service)
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                job["applied_services"] = applied
                if len(applied) == 3:
                    job["state"] = "APPLIED"
        return job

    def propose(self, request, request_id):
        identifier(request_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
            raise ControlError("INVALID_IDEMPOTENCY_KEY")
        try:
            canonical(request)
            REQUEST_VALIDATOR.validate(request)
        except (ValueError, ValidationError) as error:
            raise ControlError("INVALID_CALIBRATION_REQUEST") from error
        request_hash = digest(request)
        key = digest(request_id)
        old = self.state["request_digests"].get(key)
        if old is not None:
            if old != request_hash:
                raise ControlConflict("IDEMPOTENCY_CONFLICT")
            if request_id not in self.state["requests"]:
                raise ControlConflict("REQUEST_HISTORY_EXPIRED")
            return self.job(request_id)
        cfg = self.state["active_registration"]
        if any(request[k] != cfg[k] for k in ("site_id", "edge_id")):
            raise ControlError("DEVICE_SCOPE_MISMATCH", 403)
        if request["coordinate_frame_id"] != cfg["coordinate_frame"]["frame_id"]:
            raise ControlConflict("COORDINATE_FRAME_MISMATCH")
        if request["base_config_revision"] != cfg["config_revision"]:
            raise ControlConflict("BASE_REVISION_MISMATCH")
        if self.state["pending"] is not None or (
            self.state["active_job"] is not None
            and self.job(self.state["active_job"])["state"] != "APPLIED"
        ):
            raise ControlConflict("CONFIGURATION_APPLY_IN_PROGRESS")
        candidate = deepcopy(cfg)
        candidate.update(
            message_id=str(uuid4()),
            generated_at=utc_now(),
            config_revision=request["config_revision"],
        )
        sensors = {s["sensor_id"]: s for s in candidate["sensors"]}
        updates = request["calibrations"]
        if len({s["sensor_id"] for s in updates}) != len(updates):
            raise ControlError("DUPLICATE_SENSOR")
        for update in updates:
            if update["sensor_id"] not in sensors:
                raise ControlError("UNREGISTERED_SENSOR")
            sensors[update["sensor_id"]]["calibration"] = deepcopy(update["calibration"])
        try:
            validate_message(candidate)
        except ValueError as error:
            raise ControlError("INVALID_CALIBRATION") from error
        if candidate["config_revision"] in self.state["revision_digests"]:
            raise ControlConflict("CONFIGURATION_VERSION_REUSED")
        calibrations = self._calibration_hashes(candidate)
        for version, content in calibrations.items():
            if (
                version in self.state["calibration_digests"]
                and self.state["calibration_digests"][version] != content
            ):
                raise ControlConflict("CALIBRATION_VERSION_REUSED")
        state = deepcopy(self.state)
        state["calibration_digests"].update(calibrations)
        state["revision_digests"][candidate["config_revision"]] = registration_digest(candidate)
        state["request_digests"][key] = request_hash
        if (
            len(state["revision_digests"]) > 128
            or len(state["calibration_digests"]) > 256
            or len(state["request_digests"]) > 128
        ):
            raise ControlError("CONTROL_VERSION_HISTORY_FULL", 507)
        while len(state["requests"]) >= 64:
            oldest = next(key for key in state["requests"] if key != state["active_job"])
            del state["requests"][oldest]
        state["requests"][request_id] = {
            "state": "PENDING_BACKEND_ACK",
            "config_revision": candidate["config_revision"],
            "message_id": candidate["message_id"],
            "received_at": utc_now(),
            "reason_code": None,
        }
        state["pending"] = {
            "job_id": request_id,
            "base_revision": cfg["config_revision"],
            "registration": candidate,
        }
        self._commit(state)
        return self.job(request_id)

    async def advance(self, delivery):
        pending = self.state["pending"]
        if pending is None:
            return
        registration = deepcopy(pending["registration"])
        # Replaying the exact registration also recovers an ACK lost from the
        # uplink's bounded recent receipt journal after a controller restart.
        await delivery.enqueue(registration)
        receipt = await delivery.inspect(registration["message_id"])
        if not receipt["remote_acked"] and receipt["state"] != "quarantine":
            return
        state = deepcopy(self.state)
        job = state["requests"][pending["job_id"]]
        if receipt["state"] == "quarantine":
            job.update(state="REJECTED", reason_code="BACKEND_CONFIGURATION_REJECTED")
        elif state["active_registration"]["config_revision"] != pending["base_revision"]:
            job.update(state="REJECTED", reason_code="BASE_REVISION_MISMATCH")
        else:
            boot = Path(self.base["clock_domain_file"]).read_text().strip()
            identifier(boot)
            state.update(
                active_registration=registration,
                active_job=pending["job_id"],
                activation={
                    "clock_domain_id": boot,
                    "monotonic_ns": str(time.monotonic_ns()),
                    "activated_at": utc_now(),
                },
            )
            job.update(state="APPLYING", reason_code=None, backend_acked_at=utc_now())
        state["pending"] = None
        self._commit(state)
