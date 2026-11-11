"""Single-writer atomic-file outbox with a conservative physical-file budget.

Dedicated v2 directory only. Temporary replacement files are reserved before writing.
No SQLite WAL, implicit v1 migration, or time-based expiry of pending messages.
"""

import asyncio
import hashlib
import heapq
import json
import os
import shutil
import time
from collections import Counter, deque
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from .contracts import validate_message
from .processing import canonical, identifier
from .storage_executor import StorageExecutor


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
        self._state_counts = Counter()
        self._pending_bytes = 0
        self._acked = set()
        self._reference_cache = None
        self._ready_old, self._ready_new, self._ready_config = [], [], []
        self._ordered_periodic, self._other_old, self._other_new = [], [], []
        self._ready_live = {"PERIODIC": [], "STATE": []}
        self._queue_version = 0
        self.operation_metrics = {}
        self.recent_storage_stalls = deque(maxlen=16)
        self._realtime_stats = None
        self._realtime_loss = None
        self._storage_observation_epoch = 0
        self.inflight = set()
        self.executor = None
        self._file_allocations = {}
        self._allocated_bytes = 0
        self._usage_valid = False
        self._last_usage_audit = float("-inf")
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
            self._realtime_loss = self._loss_stats()
            self._realtime_stats = {
                **deepcopy(self.stats(audit=False)),
                "storage_observation_epoch": 0,
            }
            self.executor = StorageExecutor()
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

    async def call(self, method, *args, _priority=None, **kwargs):
        """One durable writer; periodic work can pass queued background work."""
        priority = 2 if _priority is None else _priority
        if method == "enqueue" and args and args[0].get("delivery_kind") == "PERIODIC":
            priority = 0
        elif method == "next_ordered_many":
            priority = 1
        future = self.executor.submit_priority(
            priority, partial(self._timed_call, method, time.monotonic(), args, kwargs)
        )
        return await asyncio.wrap_future(future)

    def _timed_call(self, method, queued, args, kwargs):
        started = time.monotonic()
        completed = False
        try:
            result = getattr(self, method)(*args, **kwargs)
            completed = True
            return result
        finally:
            elapsed = (time.monotonic() - started) * 1000
            wait = (started - queued) * 1000
            metric = self.operation_metrics.setdefault(
                method,
                {
                    "calls": 0,
                    "run_ms": 0.0,
                    "queue_ms": 0.0,
                    "max_run_ms": 0.0,
                    "max_queue_ms": 0.0,
                },
            )
            metric["calls"] += 1
            metric["run_ms"] += elapsed
            metric["queue_ms"] += wait
            metric["max_run_ms"] = max(metric["max_run_ms"], elapsed)
            metric["max_queue_ms"] = max(metric["max_queue_ms"], wait)
            try:
                # Publish only owned, completed state, never partially mutated metadata.
                snapshot = deepcopy(self.stats(audit=False)) if completed else None
            except BaseException:
                self._realtime_stats = None
                completed = False
                raise
            finally:
                total_elapsed = (time.monotonic() - started) * 1000
                metric["run_ms"] += total_elapsed - elapsed
                metric["max_run_ms"] = max(metric["max_run_ms"], total_elapsed)
                if max(total_elapsed, wait) >= 250:
                    self.recent_storage_stalls.append(
                        {
                            "method": method,
                            "completed_at": datetime.now(UTC).isoformat(),
                            "run_ms": total_elapsed,
                            "queue_ms": wait,
                            "completed": completed,
                        }
                    )
                if not completed:
                    self._storage_observation_epoch += 1
                if completed:
                    snapshot["storage_metrics"] = deepcopy(self.operation_metrics)
                    snapshot["recent_storage_stalls"] = deepcopy(list(self.recent_storage_stalls))
                    snapshot["storage_observation_epoch"] = self._storage_observation_epoch
                    self._realtime_stats = snapshot
                else:
                    self._realtime_stats = None

    def _loss_stats(self):
        return deepcopy(
            {
                key: self.meta[key]
                for key in (
                    "lost_messages",
                    "loss_from",
                    "loss_to",
                    "last_loss_reason",
                    "unavailable_evidence_scan_ids",
                )
            }
        )

    def realtime_stats(self):
        """Completed queue gauges plus immediately observed loss/reference state.

        The recovery observation gap bounds evaluations, not individual writes.
        Independent service and ACK timestamps still bound progress freshness.
        """
        snapshot = self._realtime_stats
        if snapshot is None:
            raise RuntimeError("storage observation unavailable")
        result = deepcopy(snapshot)
        result.update(deepcopy(self._realtime_loss))
        return result

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
        if not self._usage_valid or time.monotonic() - self._last_usage_audit >= 60:
            self._audit_usage()
        return self._allocated_bytes

    def _audit_usage(self):
        """Reconcile at startup/reporting, never enumerate the spool on every write."""
        self._usage_valid = False
        allocations = {}
        with os.scandir(self.root) as entries:
            for entry in entries:
                if entry.is_file():
                    path = Path(entry.path)
                    # Windows directory-entry sizes can lag an open file's
                    # flushed writes; inspect the file itself for quota checks.
                    info = path.stat()
                    allocations[path] = max(
                        self._allocation(info.st_size), getattr(info, "st_blocks", 0) * 512
                    )
        self._file_allocations = allocations
        self._allocated_bytes = sum(allocations.values())
        self._usage_valid = True
        self._last_usage_audit = time.monotonic()

    def _track_allocation(self, path):
        try:
            info = path.stat()
            amount = max(self._allocation(info.st_size), getattr(info, "st_blocks", 0) * 512)
        except FileNotFoundError:
            amount = 0
        except OSError:
            self._usage_valid = False
            raise
        previous = self._file_allocations.pop(path, 0)
        self._allocated_bytes += amount - previous
        if amount:
            self._file_allocations[path] = amount

    def _forget_allocation(self, path):
        self._allocated_bytes -= self._file_allocations.pop(path, 0)

    def _sync_directory(self):
        if os.name != "nt":
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _atomic(self, path, value, *, reserved=False, sync=True, encoded=None):
        data = canonical(value) if encoded is None else encoded
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
            self._track_allocation(temporary)
            if self._spool_bytes() > ceiling:
                raise CapacityError("physical temporary allocation exceeds reservation")
            os.replace(temporary, path)
            self._forget_allocation(temporary)
            self._track_allocation(path)
            if sync:
                self._sync_directory()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                self._track_allocation(temporary)

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

    def _index(self, path, record, *, payload_bytes=None):
        payload = record["payload"]
        if path.name.endswith(".acked.record"):
            record = {**record, "state": "acked"}
        receipt = self.meta["receipts"].get(payload["message_id"])
        if receipt is not None:
            if receipt["digest"] != record["digest"]:
                raise ValueError("persisted ACK conflicts with record")
            if record["state"] == "pending":
                record = {**record, "state": "acked"}
        item = {key: value for key, value in record.items() if key != "payload"}
        item.update(
            path=path,
            kind=payload["message_type"],
            delivery_kind=payload.get("delivery_kind"),
            assessment_type=payload.get("assessment_type"),
            scans=self._scans(payload),
            generated_at=payload["generated_at"],
            payload_bytes=len(canonical(payload)) if payload_bytes is None else payload_bytes,
        )
        mid = payload["message_id"]
        if mid in self.index:
            self._count_item(self.index[mid], -1)
        self.index[mid] = item
        self._count_item(item, 1)
        if record["state"] == "acked":
            self._acked.add(mid)
        else:
            self._acked.discard(mid)
        if payload["message_type"] == "edge_assessment":
            self._reference_cache = None
        if record["state"] == "pending":
            self._queue_pending(mid)

    def _count_item(self, item, direction):
        self._state_counts[item["state"]] += direction
        if item["state"] == "pending":
            self._pending_bytes += direction * item["payload_bytes"]

    def _mark_acked(self, mid, item):
        if item["state"] != "acked":
            self._count_item(item, -1)
            item["state"] = "acked"
            self._count_item(item, 1)
        self._acked.add(mid)
        if item["kind"] == "edge_assessment":
            self._reference_cache = None

    def _queue_pending(self, mid):
        item = self.index[mid]
        self._queue_version += 1
        item["queue_version"] = self._queue_version
        entry = (item["order"], self._queue_version, mid)
        heapq.heappush(self._ready_old, entry)
        heapq.heappush(self._ready_new, (-entry[0], entry[1], mid))
        if item["kind"] == "edge_configuration":
            heapq.heappush(self._ready_config, entry)
        live_kind = item["delivery_kind"] or item["assessment_type"]
        if live_kind in self._ready_live:
            heapq.heappush(self._ready_live[live_kind], (-entry[0], entry[1], mid))
        if item["delivery_kind"] == "PERIODIC":
            heapq.heappush(self._ordered_periodic, entry)
        else:
            heapq.heappush(self._other_old, entry)
            heapq.heappush(self._other_new, (-entry[0], entry[1], mid))
        self._compact_ready()

    def _compact_ready(self):
        # Lazy invalid entries are bounded, including during long retry loops.
        if (
            max(
                len(heap)
                for heap in (
                    self._ready_old,
                    self._ready_new,
                    self._ready_config,
                    *self._ready_live.values(),
                    self._ordered_periodic,
                    self._other_old,
                    self._other_new,
                )
            )
            <= 2 * len(self.index) + 64
        ):
            return
        entries = [
            (v["order"], v["queue_version"], mid)
            for mid, v in self.index.items()
            if v["state"] == "pending" and mid not in self.inflight
        ]
        self._ready_old = entries
        self._ready_new = [(-order, version, mid) for order, version, mid in entries]
        self._ready_config = [
            entry for entry in entries if self.index[entry[2]]["kind"] == "edge_configuration"
        ]
        for kind in self._ready_live:
            self._ready_live[kind] = [
                (-order, version, mid)
                for order, version, mid in entries
                if (self.index[mid]["delivery_kind"] or self.index[mid]["assessment_type"]) == kind
            ]
        self._ordered_periodic = [
            e for e in entries if self.index[e[2]]["delivery_kind"] == "PERIODIC"
        ]
        self._other_old = [e for e in entries if self.index[e[2]]["delivery_kind"] != "PERIODIC"]
        self._other_new = [(-o, v, m) for o, v, m in self._other_old]
        for heap in (
            self._ready_old,
            self._ready_new,
            self._ready_config,
            *self._ready_live.values(),
            self._ordered_periodic,
            self._other_old,
            self._other_new,
        ):
            heapq.heapify(heap)

    def _ready(self, heap):
        while heap:
            _, version, mid = heap[0]
            item = self.index.get(mid)
            if (
                item is not None
                and item["state"] == "pending"
                and item.get("queue_version") == version
                and mid not in self.inflight
            ):
                return mid
            heapq.heappop(heap)
        return None

    def get(self, message_id):
        item = self.index.get(message_id)
        if item is None:
            return None
        record = json.loads(item["path"].read_bytes())
        if record["state"] == "pending" and (
            message_id in self.meta["receipts"] or item["path"].name.endswith(".acked.record")
        ):
            record["state"] = "acked"
        return record

    def enqueue(self, payload, *, sync=True, restore_acked=False):
        encoded = validate_message(payload)
        return self._enqueue_validated(payload, encoded, sync=sync, restore_acked=restore_acked)

    def _enqueue_validated(self, payload, encoded, *, sync, restore_acked=False):
        mid = payload["message_id"]
        digest = hashlib.sha256(encoded).hexdigest()
        previous = self.index.get(mid) or self.meta["receipts"].get(mid)
        if previous:
            if previous["digest"] != digest:
                raise ConflictError("same message ID has different content")
            if not (restore_acked and mid not in self.index):
                return True
        record = {
            "payload": payload,
            "digest": digest,
            "state": "acked" if mid in self.meta["receipts"] else "pending",
            "reason": None,
            "order": time.time_ns(),
        }
        # Reuse exactly the validated canonical payload bytes. Only the small
        # record envelope is new; encoding its point arrays again is redundant.
        record_bytes = (
            b"{"
            + b",".join(
                canonical(key) + b":" + (encoded if key == "payload" else canonical(record[key]))
                for key in sorted(record)
            )
            + b"}"
        )
        needed = self._allocation(len(record_bytes))
        try:
            self._make_room(needed)
            path = self._path(mid)
            self._atomic(path, record, sync=sync, encoded=record_bytes)
            self._index(path, record, payload_bytes=len(encoded))
        except (CapacityError, OSError):
            self._loss(payload, "STORAGE_FULL")
            raise CapacityError("message was not stored") from None
        return False

    @staticmethod
    def _validate_bundle(messages):
        if not isinstance(messages, list) or not 1 <= len(messages) <= 17:
            raise ValueError("invalid judgment bundle size")
        for payload in messages:
            validate_message(payload)
        assessment = messages[-1]
        if assessment["message_type"] != "edge_assessment":
            raise ValueError("bundle must end with assessment")
        if len({p["message_id"] for p in messages}) != len(messages):
            raise ValueError("duplicate bundle message ID")
        fields = (
            "scan_id",
            "sensor_id",
            "config_revision",
            "calibration_version",
            "coordinate_frame_id",
        )
        required = {tuple(r[k] for k in fields) for r in assessment["evidence"]["scans"]}
        supplied = set()
        for payload in messages[:-1]:
            if (
                payload["message_type"] != "lidar_measurement"
                or payload["delivery_kind"] != "EVIDENCE"
                or payload["assessment_ids"] != [assessment["assessment_id"]]
                or any(
                    payload[k] != assessment[k] for k in ("site_id", "edge_id", "config_revision")
                )
            ):
                raise ValueError("invalid judgment evidence envelope")
            for sensor in payload["sensors"]:
                if sensor["scan"]:
                    supplied.add(
                        (
                            sensor["scan"]["scan_id"],
                            sensor["sensor_id"],
                            payload["config_revision"],
                            sensor["calibration_version"],
                            payload["coordinate_frame_id"],
                        )
                    )
        if required != supplied:
            raise ValueError("bundle evidence does not match judgment references")
        return assessment, required

    def enqueue_bundle(self, messages):
        """Local convenience; streaming IPC uses begin/enqueue/commit with the same protection."""
        assessment, required = self._validate_bundle(messages)
        if not required:
            return self.enqueue(assessment)
        manifest = [
            {"message_id": p["message_id"], "digest": hashlib.sha256(canonical(p)).hexdigest()}
            for p in messages[:-1]
        ]
        self.begin_bundle(assessment, manifest)
        try:
            for payload in messages[:-1]:
                self.enqueue(payload)
            return self.commit_bundle(assessment)
        except BaseException:
            self.release(assessment["message_id"])
            raise

    def begin_bundle(self, assessment, manifest):
        validate_message(assessment)
        if assessment["message_type"] != "edge_assessment":
            raise ValueError("bundle must end with assessment")
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 16:
            raise ValueError("invalid evidence manifest")
        expected = {}
        for row in manifest:
            identifier(row["message_id"])
            digest = row["digest"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or row["message_id"] in expected
                or row["message_id"] == assessment["message_id"]
            ):
                raise ValueError("invalid evidence digest or identity")
            expected[row["message_id"]] = digest
        scan_ids = [r["scan_id"] for r in assessment["evidence"]["scans"]]
        if not scan_ids or len(scan_ids) > 32:
            raise ValueError("invalid bundle references")
        owner = assessment["message_id"]
        digest = hashlib.sha256(canonical(assessment)).hexdigest()
        previous = self.meta["leases"].get(owner)
        if previous and (
            previous.get("assessment_digest") != digest
            or previous.get("evidence_manifest") != expected
        ):
            raise ConflictError("bundle identity conflict")
        if previous is None and len(self.meta["leases"]) >= 16:
            raise CapacityError("active reference limit")
        self.meta["leases"][owner] = {
            "scans": sorted(set(scan_ids)),
            "expires_at": time.time() + 300,
            "assessment_digest": digest,
            "evidence_manifest": expected,
        }
        try:
            self._save_meta()
        except BaseException:
            if previous is None:
                self.meta["leases"].pop(owner, None)
            else:
                self.meta["leases"][owner] = previous
            raise

    def commit_bundle(self, assessment):
        validate_message(assessment)
        owner = assessment["message_id"]
        lease = self.meta["leases"].get(owner)
        if (
            lease is None
            or lease["expires_at"] <= time.time()
            or lease.get("assessment_digest") != hashlib.sha256(canonical(assessment)).hexdigest()
        ):
            raise ValueError("bundle preparation missing, expired or changed")
        try:
            fields = (
                "scan_id",
                "sensor_id",
                "config_revision",
                "calibration_version",
                "coordinate_frame_id",
            )
            required = {tuple(r[k] for k in fields) for r in assessment["evidence"]["scans"]}
            supplied = set()
            for mid, digest in lease["evidence_manifest"].items():
                record = self.get(mid)
                if record is None or record["state"] == "quarantine":
                    raise ValueError("bundle evidence unavailable")
                payload = record["payload"]
                if (
                    hashlib.sha256(canonical(payload)).hexdigest() != digest
                    or payload["message_type"] != "lidar_measurement"
                    or payload["delivery_kind"] != "EVIDENCE"
                    or payload["assessment_ids"] != [assessment["assessment_id"]]
                    or any(
                        payload[k] != assessment[k]
                        for k in ("site_id", "edge_id", "config_revision")
                    )
                ):
                    raise ValueError("bundle evidence identity mismatch")
                for s in payload["sensors"]:
                    if s["scan"]:
                        supplied.add(
                            (
                                s["scan"]["scan_id"],
                                s["sensor_id"],
                                payload["config_revision"],
                                s["calibration_version"],
                                payload["coordinate_frame_id"],
                            )
                        )
            if supplied != required:
                raise ValueError("bundle evidence references mismatch")
            return self.enqueue(assessment)
        finally:
            self.release(owner)

    def stage_bundle(self, assessment, payloads):
        """Fsync each immutable file, then one directory barrier before the RPC ACK."""
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= 4:
            raise ValueError("invalid bounded evidence batch")
        owner = assessment["message_id"]
        lease = self.meta["leases"].get(owner)
        if (
            lease is None
            or lease["expires_at"] <= time.time()
            or lease.get("assessment_digest") != hashlib.sha256(canonical(assessment)).hexdigest()
        ):
            raise ValueError("bundle preparation missing, expired or changed")
        prepared = []
        for payload in payloads:
            encoded = validate_message(payload)
            if (
                payload.get("message_type") != "lidar_measurement"
                or payload.get("delivery_kind") != "EVIDENCE"
                or payload.get("assessment_ids") != [assessment["assessment_id"]]
                or any(
                    payload.get(k) != assessment[k]
                    for k in ("site_id", "edge_id", "config_revision")
                )
                or lease["evidence_manifest"].get(payload["message_id"])
                != hashlib.sha256(encoded).hexdigest()
            ):
                raise ValueError("evidence batch identity mismatch")
            prepared.append((payload, encoded))
        try:
            for payload, encoded in prepared:
                # A lost/expired preparation lease may have allowed collection
                # of an ACKed body. Recreate its exact hash-matched evidence for
                # local commit validation without losing the proven remote ACK.
                self._enqueue_validated(payload, encoded, sync=False, restore_acked=True)
        finally:
            # Partial writes remain replayable and protected by the persisted
            # lease; never acknowledge the batch before this barrier succeeds.
            self._sync_directory()

    def _protected(self, now=None):
        now = time.time() if now is None else now
        active = {
            scan
            for lease in self.meta["leases"].values()
            if lease["expires_at"] > now
            for scan in lease["scans"]
        }
        if self._reference_cache is None:
            self._reference_cache = {
                scan
                for item in self.index.values()
                if item["kind"] == "edge_assessment" and item["state"] != "acked"
                for scan in item["scans"]
            }
        return active, self._reference_cache

    def _delete(self, mid, *, sync=True):
        path = self.index[mid]["path"]
        path.unlink()
        self._forget_allocation(path)
        if self.index[mid]["kind"] == "edge_assessment":
            self._reference_cache = None
        self._acked.discard(mid)
        self._count_item(self.index[mid], -1)
        del self.index[mid]
        self._compact_ready()
        if sync:
            self._sync_directory()

    def _make_room(self, needed, *, exclude=(), now=None):
        # ACK handling and the periodic collector reclaim eligible records.
        # A write with sufficient room need not traverse protected ACKs again.
        if self._spool_bytes() + needed <= self.limit - self.external_reserve - self.reserve:
            return
        self.collect(now=now)
        excluded = self.inflight | set(exclude)
        while self._spool_bytes() + needed > self.limit - self.external_reserve - self.reserve:
            active, referenced = self._protected(now)
            protected = active | referenced
            candidates = [
                (mid, item)
                for mid, item in self.index.items()
                if mid not in excluded
                and item["delivery_kind"] == "PERIODIC"
                and not item["scans"] & protected
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
                    and not item["scans"] & protected
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
        # Expose loss before any slow persistence or eviction. Never let an
        # in-flight write hide a changed loss count from recovery evaluation.
        self._realtime_loss = self._loss_stats()
        self._save_meta()

    def next_ready(self, *, pin=False):
        mid = self._ready(self._ready_config)
        if mid is None:
            mid = self._ready(self._ready_new if self.latest else self._ready_old)
            if mid is None:
                return None
            self.latest = not self.latest
        if pin:
            self.inflight.add(mid)
        return self.get(mid)

    def unpin(self, mid):
        pinned = mid in self.inflight
        self.inflight.discard(mid)
        if pinned and mid in self.index and self.index[mid]["state"] == "pending":
            self._queue_pending(mid)

    def next_ready_many(self, limit=16):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("invalid selection batch")
        before = set(self.inflight)
        rows = []
        live = list(self._ready_live.values())
        try:
            for _ in range(limit):
                mid = None
                # Configuration stays first. Reserve one opportunity per turn
                # for current original scans and judgments during old backlog.
                if self._ready(self._ready_config) is None:
                    while live and mid is None:
                        mid = self._ready(live.pop(0))
                if mid is None:
                    row = self.next_ready(pin=True)
                else:
                    self.inflight.add(mid)
                    row = self.get(mid)
                if row is None:
                    break
                rows.append(row)
            return rows
        except BaseException:
            self.unpin_many(tuple(self.inflight - before))
            raise

    def next_ordered_many(self, limit=16):
        """One oldest periodic reservation; other message lanes remain concurrent."""
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("invalid selection batch")
        before = set(self.inflight)
        periodic_busy = any(
            self.index[m]["delivery_kind"] == "PERIODIC" for m in before if m in self.index
        )
        rows = []
        state_reserved = False
        try:
            for _ in range(limit):
                mid = self._ready(self._ready_config)
                if mid is None and not periodic_busy:
                    mid = self._ready(self._ordered_periodic)
                    if mid is not None:
                        periodic_busy = True
                if mid is None and not state_reserved:
                    mid = self._ready(self._ready_live["STATE"])
                    state_reserved = True
                if mid is None:
                    mid = self._ready(self._other_new if self.latest else self._other_old)
                    self.latest = not self.latest
                if mid is None:
                    break
                self.inflight.add(mid)
                rows.append(self.get(mid))
            return rows
        except BaseException:
            self.unpin_many(tuple(self.inflight - before))
            raise

    def unpin_many(self, mids):
        if len(mids) > 16:
            raise ValueError("invalid release batch")
        for mid in mids:
            self.unpin(mid)

    def _change(self, mid, *, now=None, **changes):
        record = self.get(mid)
        record.update(changes)
        # Updates need a second copy; reserve remains available for diagnostics only.
        self._make_room(self._allocation(len(canonical(record))), exclude=(mid,), now=now)
        if mid not in self.index:
            raise CapacityError("record evicted during update")
        path = self.index[mid]["path"]
        self._atomic(path, record)
        self._index(path, record)

    def quarantine(self, mid, reason):
        self._change(mid, state="quarantine", reason=reason)

    def delivered(self, mid, received_at, *, now=None):
        self.delivered_many([(mid, received_at)], now=now)

    def delivered_many(self, receipts, *, now=None, release_pins=False):
        if not 1 <= len(receipts) <= 8 or len({mid for mid, _ in receipts}) != len(receipts):
            raise ValueError("invalid bounded ACK batch")
        items = {mid: self.index[mid] for mid, _ in receipts}
        times = [at for _, at in receipts]
        if self.meta["last_backend_ack_at"] is not None:
            times.append(self.meta["last_backend_ack_at"])
        newest = max(times, key=datetime.fromisoformat)
        previous = deepcopy(self.meta)
        try:
            self.meta["last_backend_ack_at"] = newest
            for mid, item in items.items():
                self.meta["receipts"][mid] = {"digest": item["digest"]}
            while len(self.meta["receipts"]) > 32:
                oldest = next(iter(self.meta["receipts"]))
                retained = self.index.get(oldest)
                if retained is not None:
                    # The old receipt already durably proves this ACK. Rename
                    # the immutable body to carry that state without allocating
                    # a second body at full protected capacity. Sync the rename
                    # before removing the receipt. Both crash orders retain proof.
                    path = retained["path"]
                    if not path.name.endswith(".acked.record"):
                        target = path.with_suffix(".acked.record")
                        os.replace(path, target)
                        # The filesystem move has happened even if inspecting
                        # its allocation fails; keep retries pointed at the body.
                        retained["path"] = target
                        self._mark_acked(oldest, retained)
                        self._forget_allocation(path)
                        self._track_allocation(target)
                    self._sync_directory()
                del self.meta["receipts"][oldest]
            self._save_meta()
        except BaseException:
            self.meta = previous
            raise
        # The fsynced receipt is authoritative across restart; do not rewrite
        # a large protected scan for every successful HTTP acknowledgement.
        for mid, item in items.items():
            self._mark_acked(mid, item)
            if release_pins:
                self.inflight.discard(mid)
            if item["kind"] == "edge_assessment":
                self._reference_cache = None
        active, referenced = self._protected(now)
        protected = active | referenced
        changed = False
        for mid, item in items.items():
            if not item["scans"] & protected:
                self._delete(mid, sync=False)
                changed = True
        changed = self.collect(now=now, sync=False) or changed
        if changed:
            self._sync_directory()

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

    def collect(self, *, now=None, sync=True):
        current = time.time() if now is None else now
        expired = [
            owner for owner, lease in self.meta["leases"].items() if lease["expires_at"] <= current
        ]
        if expired:
            for owner in expired:
                del self.meta["leases"][owner]
            self._save_meta()
        active, referenced = self._protected(now)
        protected = active | referenced
        changed = False
        for mid in tuple(self._acked):
            item = self.index[mid]
            if (
                mid not in self.inflight
                and item["state"] == "acked"
                and not item["scans"] & protected
            ):
                self._delete(mid, sync=False)
                changed = True
        if changed and sync:
            self._sync_directory()
        return changed

    def stats(self, *, audit=True):
        # Administrative callers can force reconciliation. Frequent runtime
        # reports use incrementally tracked allocations, audited at most 60s
        # apart by _spool_bytes (also checked before every physical write).
        if audit:
            self._audit_usage()
        return {
            "storage_metrics": deepcopy(self.operation_metrics),
            "recent_storage_stalls": deepcopy(list(self.recent_storage_stalls)),
            "pending_messages": self._state_counts["pending"],
            "pending_bytes": self._pending_bytes,
            "quarantined_messages": self._state_counts["quarantine"],
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
