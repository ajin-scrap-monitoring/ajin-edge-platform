"""Single-writer atomic-file outbox with a conservative physical-file budget.

Dedicated v2 directory only. Temporary replacement files are reserved before writing.
No SQLite WAL, implicit v1 migration, or time-based expiry of pending messages.
"""

import asyncio
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from .contracts import validate_message
from .processing import canonical


class CapacityError(RuntimeError):
    pass


class ConflictError(ValueError):
    pass


class Outbox:
    def __init__(
        self,
        directory,
        *,
        limit_bytes=4294967296,
        reserve_bytes=65536,
        external_reserve_bytes=0,
        external_usage=None,
    ):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        if any(
            p.name not in ("metadata.json", ".writer.lock")
            and not (p.name.endswith(".record") or p.name.endswith(".tmp"))
            for p in self.root.iterdir()
        ):
            raise ValueError("v2 requires a dedicated directory; existing data left untouched")
        if (
            external_reserve_bytes < 0
            or limit_bytes <= reserve_bytes + external_reserve_bytes + 16384
        ):
            raise ValueError("insufficient storage budget")
        self.limit, self.reserve = limit_bytes, reserve_bytes
        self.external_reserve = external_reserve_bytes
        self.external_usage = external_usage or (lambda: 0)
        self.lock = (self.root / ".writer.lock").open("a+b")
        self._lock()
        self.latest = True
        self.meta = {
            "version": 2,
            "lost_messages": 0,
            "loss_from": None,
            "loss_to": None,
            "last_loss_reason": None,
            "unavailable_evidence_scan_ids": [],
            "last_backend_ack_at": None,
            "leases": {},
            "receipts": {},
        }
        self.index = {}
        self.inflight = set()
        self.executor = None
        try:
            if (self.root / "metadata.json").exists():
                self.meta = json.loads((self.root / "metadata.json").read_bytes())
                if self.meta["version"] != 2:
                    raise ValueError("incompatible storage format")
            for path in self.root.glob("*.record"):
                self._index(path, json.loads(path.read_bytes()))
            # Only our uncommitted replacements; never remove committed records on startup.
            for path in self.root.glob("*.tmp"):
                path.unlink()
            if self.used_bytes() > self.limit:
                raise CapacityError("existing directory exceeds configured budget")
            self._save_meta()
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v2-storage")
        except BaseException:
            self.close()
            raise

    def _lock(self):
        if os.name == "nt":
            import msvcrt

            self.lock.seek(0)
            if self.lock.read(1) == b"":
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def close(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
        if not self.lock.closed:
            self.lock.close()

    async def call(self, method, *args, **kwargs):
        """Serialize blocking filesystem work outside the service event loop."""
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, partial(getattr(self, method), *args, **kwargs)
        )

    def inspect(self, mid):
        item = self.index.get(mid)
        acked = mid in self.meta["receipts"] or bool(item and item["state"] == "acked")
        return {
            "message_id": mid,
            "remote_acked": acked,
            "state": "acked" if acked else item["state"] if item else "unknown",
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _allocation(size):
        return ((size + 4095) // 4096) * 4096

    def used_bytes(self):
        return self._spool_bytes() + self.external_usage()

    def _spool_bytes(self):
        total = 0
        for path in self.root.iterdir():
            if path.is_file():
                stat = path.stat()
                total += max(self._allocation(stat.st_size), getattr(stat, "st_blocks", 0) * 512)
        return total

    def _sync_directory(self):
        if os.name != "nt":
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _atomic(self, path, value, *, reserved=False):
        data = canonical(value)
        needed = self._allocation(len(data))
        ceiling = self.limit - self.external_reserve - (0 if reserved else self.reserve)
        if (
            self._spool_bytes() + needed > ceiling
            or self.external_usage() > self.external_reserve
            or shutil.disk_usage(self.root).free < needed + self.reserve
        ):
            raise CapacityError("physical storage reservation failed")
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("xb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _save_meta(self):
        if self._allocation(len(canonical(self.meta))) * 2 > self.reserve:
            raise CapacityError("diagnostic/reference reserve exhausted")
        self._atomic(self.root / "metadata.json", self.meta, reserved=True)

    def _path(self, message_id):
        return self.root / (hashlib.sha256(message_id.encode()).hexdigest() + ".record")

    @staticmethod
    def _scans(payload):
        if payload["message_type"] == "lidar_measurement":
            return {s["scan"]["scan_id"] for s in payload["sensors"] if s["scan"]}
        if payload["message_type"] == "edge_assessment":
            return {s["scan_id"] for s in payload["evidence"]["scans"]}
        return set()

    def _index(self, path, record):
        payload = record["payload"]
        self.index[payload["message_id"]] = {
            key: value for key, value in record.items() if key != "payload"
        }
        self.index[payload["message_id"]].update(
            path=path,
            kind=payload["message_type"],
            delivery_kind=payload.get("delivery_kind"),
            scans=self._scans(payload),
            generated_at=payload["generated_at"],
            payload_bytes=len(canonical(payload)),
        )

    def get(self, message_id):
        item = self.index.get(message_id)
        return json.loads(item["path"].read_bytes()) if item else None

    def enqueue(self, payload):
        validate_message(payload)
        mid = payload["message_id"]
        digest = hashlib.sha256(canonical(payload)).hexdigest()
        previous = self.index.get(mid) or self.meta["receipts"].get(mid)
        if previous:
            if previous["digest"] != digest:
                raise ConflictError("same message ID has different content")
            return True
        record = {
            "payload": payload,
            "digest": digest,
            "state": "pending",
            "reason": None,
            "order": time.time_ns(),
        }
        needed = self._allocation(len(canonical(record)))
        try:
            self._make_room(needed)
            path = self._path(mid)
            self._atomic(path, record)
            self._index(path, record)
        except (CapacityError, OSError):
            self._loss(payload, "STORAGE_FULL")
            raise CapacityError("message was not stored") from None
        return False

    def _protected(self, now=None):
        now = time.time() if now is None else now
        active = {
            scan
            for lease in self.meta["leases"].values()
            if lease["expires_at"] > now
            for scan in lease["scans"]
        }
        referenced = {
            scan
            for item in self.index.values()
            if item["kind"] == "edge_assessment" and item["state"] != "acked"
            for scan in item["scans"]
        }
        return active, referenced

    def _delete(self, mid):
        self.index[mid]["path"].unlink()
        del self.index[mid]
        self._sync_directory()

    def _make_room(self, needed, *, exclude=(), now=None):
        self.collect(now=now)
        excluded = self.inflight | set(exclude)
        while self._spool_bytes() + needed > self.limit - self.external_reserve - self.reserve:
            active, referenced = self._protected(now)
            candidates = [
                (mid, item)
                for mid, item in self.index.items()
                if mid not in excluded
                and item["delivery_kind"] == "PERIODIC"
                and not item["scans"] & (active | referenced)
            ]
            if not candidates:
                candidates = [
                    (mid, item)
                    for mid, item in self.index.items()
                    if mid not in excluded
                    and item["kind"] == "edge_assessment"
                    and not item["scans"] & active
                ]
            if not candidates:
                candidates = [
                    (mid, item)
                    for mid, item in self.index.items()
                    if mid not in excluded
                    and item["delivery_kind"] == "EVIDENCE"
                    and not item["scans"] & (active | referenced)
                ]
            if not candidates:
                raise CapacityError("only protected records remain")
            mid, _ = min(candidates, key=lambda pair: pair[1]["order"])
            self._loss(self.get(mid)["payload"], "CAPACITY_EVICTION")
            self._delete(mid)
            self.collect(now=now)

    def _loss(self, payload, reason):
        self.meta["lost_messages"] += 1
        at = payload["generated_at"]
        self.meta["loss_from"] = min(self.meta["loss_from"] or at, at)
        self.meta["loss_to"] = max(self.meta["loss_to"] or at, at)
        self.meta["last_loss_reason"] = reason
        ids = self.meta["unavailable_evidence_scan_ids"] + sorted(self._scans(payload))
        self.meta["unavailable_evidence_scan_ids"] = list(dict.fromkeys(ids))[-32:]
        self._save_meta()

    def next_ready(self, *, pin=False):
        items = [(mid, item) for mid, item in self.index.items() if item["state"] == "pending"]
        if not items:
            return None
        selector = max if self.latest else min
        mid, _ = selector(items, key=lambda pair: pair[1]["order"])
        self.latest = not self.latest
        if pin:
            self.inflight.add(mid)
        return self.get(mid)

    def unpin(self, mid):
        self.inflight.discard(mid)

    def _change(self, mid, *, now=None, **changes):
        record = self.get(mid)
        record.update(changes)
        # Updates need a second copy; reserve remains available for diagnostics only.
        self._make_room(self._allocation(len(canonical(record))), exclude=(mid,), now=now)
        if mid not in self.index:
            raise CapacityError("record evicted during update")
        self._atomic(self._path(mid), record)
        self._index(self._path(mid), record)

    def quarantine(self, mid, reason):
        self._change(mid, state="quarantine", reason=reason)

    def delivered(self, mid, received_at, *, now=None):
        item = self.index[mid]
        self.meta["last_backend_ack_at"] = received_at
        self.meta["receipts"][mid] = {"digest": item["digest"]}
        while len(self.meta["receipts"]) > 32:
            del self.meta["receipts"][next(iter(self.meta["receipts"]))]
        self._save_meta()
        active, referenced = self._protected(now)
        if item["scans"] & (active | referenced):
            self._change(mid, state="acked", now=now)
        else:
            self._delete(mid)
        self.collect(now=now)

    def protect(self, owner, scan_ids, *, expires_at):
        if not owner or len(owner) > 128 or len(scan_ids) > 32:
            raise ValueError("invalid bounded lease")
        if owner not in self.meta["leases"] and len(self.meta["leases"]) >= 16:
            raise CapacityError("active reference limit")
        self.meta["leases"][owner] = {"scans": sorted(set(scan_ids)), "expires_at": expires_at}
        self._save_meta()

    def release(self, owner, *, now=None):
        self.meta["leases"].pop(owner, None)
        self._save_meta()
        self.collect(now=now)

    def collect(self, *, now=None):
        current = time.time() if now is None else now
        expired = [
            owner for owner, lease in self.meta["leases"].items() if lease["expires_at"] <= current
        ]
        if expired:
            for owner in expired:
                del self.meta["leases"][owner]
            self._save_meta()
        active, referenced = self._protected(now)
        for mid, item in list(self.index.items()):
            if (
                mid not in self.inflight
                and item["state"] == "acked"
                and not item["scans"] & (active | referenced)
            ):
                self._delete(mid)

    def stats(self):
        pending = [item for item in self.index.values() if item["state"] == "pending"]
        return {
            "pending_messages": len(pending),
            "pending_bytes": sum(item["payload_bytes"] for item in pending),
            "quarantined_messages": sum(
                item["state"] == "quarantine" for item in self.index.values()
            ),
            "last_backend_ack_at": self.meta["last_backend_ack_at"],
            "limit_bytes": self.limit,
            "used_bytes": self.used_bytes(),
            **{
                key: self.meta[key]
                for key in (
                    "lost_messages",
                    "loss_from",
                    "loss_to",
                    "last_loss_reason",
                    "unavailable_evidence_scan_ids",
                )
            },
        }
