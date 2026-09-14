"""Single-writer SQLite outbox. No ack escapes before FULL-synchronous COMMIT."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from ajin_edge.contracts import validate_measurement


class DuplicateConflict(ValueError):
    pass


class Outbox:
    def __init__(
        self,
        path,
        *,
        max_records=100_000,
        max_bytes=192 * 1024 * 1024,
        max_age_seconds=86400,
        max_quarantine=1000,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    ):
        if min(max_records, max_bytes, max_age_seconds, max_quarantine) <= 0:
            raise ValueError("outbox limits must be positive")
        self.max_records, self.max_bytes = max_records, max_bytes
        self.max_age, self.max_quarantine = max_age_seconds, max_quarantine
        self.wall_clock, self.monotonic_clock = wall_clock, monotonic_clock
        self._last_mono = monotonic_clock()
        self._last_wall = wall_clock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA auto_vacuum=FULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=2000")
        self.db.execute("PRAGMA wal_autocheckpoint=128")
        self.db.execute("PRAGMA journal_size_limit=1048576")
        # Physical database cap leaves 16 MiB for WAL/SHM under a 256 MiB volume budget.
        self.db.execute("PRAGMA max_page_count=61440")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, measurement_id TEXT UNIQUE NOT NULL,
                body BLOB NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL,
                next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS pending_time ON messages(next_attempt, ordinal);
            CREATE TABLE IF NOT EXISTS receipts (
                measurement_id TEXT PRIMARY KEY, digest TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS quarantine (
                measurement_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                reason TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "received_utc" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN received_utc REAL")
            self.db.execute("UPDATE messages SET received_utc=created")
            self.db.commit()
        saved = self.db.execute("SELECT value FROM metadata WHERE key='retention_clock'").fetchone()
        self._logical = float(saved[0]) if saved else None
        if saved is None and self.db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
            # Older databases only stored UTC ages. Begin a fresh elapsed-time
            # retention interval instead of guessing downtime or UTC corrections.
            self._logical = self._last_wall
            with self.db:
                for table in ("messages", "receipts", "quarantine"):
                    self.db.execute(f"UPDATE {table} SET created=?", (self._logical,))
                self._checkpoint_clock()
        # Re-arm persisted UTC deadlines once against this process monotonic clock.
        # Downtime/clock corrections never schedule more than 60s after startup.
        self._retry_due = {
            row["measurement_id"]: self._last_mono
            + min(60, max(0, row["next_attempt"] - self._last_wall))
            for row in self.db.execute("SELECT measurement_id,next_attempt FROM messages")
        }

    def _tick(self, now=None, monotonic_now=None):
        wall = self.wall_clock() if now is None else now
        mono = self.monotonic_clock() if monotonic_now is None else monotonic_now
        # An async caller may hold a sample older than another caller's tick.
        # Never rewind the baseline and count that elapsed interval twice.
        if mono < self._last_mono:
            mono, wall = self._last_mono, self._last_wall
        if self._logical is None:
            self._logical = wall
        else:
            self._logical += max(0, mono - self._last_mono)
        self._last_mono, self._last_wall = mono, wall
        return self._logical

    def _checkpoint_clock(self):
        self.db.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('retention_clock',?)", (str(self._logical),)
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def _loss(self, measurement_id, reason, received_utc):
        self.db.execute("""INSERT INTO metadata VALUES ('lost_records','1')
            ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1""")
        for key, value in (("last_lost_id", measurement_id), ("last_loss_reason", reason)):
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))
        self.db.execute(
            "INSERT OR IGNORE INTO metadata VALUES ('first_lost_id',?)", (measurement_id,)
        )
        for key, op in [("loss_range_start_unix", "min"), ("loss_range_end_unix", "max")]:
            old = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            value = (
                received_utc
                if old is None
                else (min if op == "min" else max)(received_utc, float(old[0]))
            )
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, str(value)))

    def _evict(self, row, reason):
        received = self.db.execute(
            "SELECT received_utc FROM messages WHERE measurement_id=?", (row["measurement_id"],)
        ).fetchone()[0]
        self.db.execute("DELETE FROM messages WHERE measurement_id=?", (row["measurement_id"],))
        self._loss(row["measurement_id"], reason, received)
        self._retry_due.pop(row["measurement_id"], None)

    def _maintain(self, now):
        self._checkpoint_clock()
        for row in self.db.execute(
            "SELECT measurement_id FROM messages WHERE created<=? ORDER BY ordinal",
            (now - self.max_age,),
        ).fetchall():
            self._evict(row, "RETENTION_EXPIRED")
        count, size = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(body)),0) FROM messages"
        ).fetchone()
        while count > self.max_records or size > self.max_bytes:
            row = self.db.execute(
                "SELECT measurement_id,length(body) AS size FROM messages ORDER BY ordinal LIMIT 1"
            ).fetchone()
            self._evict(row, "CAPACITY_EVICTED")
            count, size = count - 1, size - row["size"]
        self.db.execute("DELETE FROM receipts WHERE created<=?", (now - self.max_age,))
        self.db.execute(
            """DELETE FROM receipts WHERE measurement_id IN (
            SELECT measurement_id FROM receipts ORDER BY created DESC LIMIT -1 OFFSET ?)""",
            (self.max_records,),
        )
        self.db.execute(
            """DELETE FROM quarantine WHERE measurement_id IN (
            SELECT measurement_id FROM quarantine ORDER BY created DESC LIMIT -1 OFFSET ?)""",
            (self.max_quarantine,),
        )

    def enqueue(self, payload, *, now=None):
        raw = validate_measurement(payload)
        if len(raw) > self.max_bytes:
            raise ValueError("record exceeds outbox capacity")
        digest = hashlib.sha256(raw).hexdigest()
        mid = payload["measurement_id"]
        logical = self._tick(now)
        wall = self._last_wall
        with self.db:
            for table in ("messages", "receipts", "quarantine"):
                row = self.db.execute(
                    f"SELECT digest FROM {table} WHERE measurement_id=?", (mid,)
                ).fetchone()
                if row:
                    if row["digest"] != digest:
                        raise DuplicateConflict("measurement ID reused with different content")
                    return True
            self.db.execute(
                "INSERT INTO messages"
                "(measurement_id,body,digest,created,next_attempt,received_utc) "
                "VALUES (?,?,?,?,?,?)",
                (mid, raw, digest, logical, wall, wall),
            )
            self._retry_due[mid] = self._last_mono
            self._maintain(logical)
        return False

    def maintain(self, *, now=None):
        with self.db:
            self._maintain(self._tick(now))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def next_ready(self, *, now=None, monotonic_now=None, prefer_latest=False):
        direction = "DESC" if prefer_latest else "ASC"
        self._tick(now, monotonic_now)
        row = next(
            (
                row
                for row in self.db.execute(f"SELECT * FROM messages ORDER BY ordinal {direction}")
                if self._retry_due.get(row["measurement_id"], 0) <= self._last_mono
            ),
            None,
        )
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("body"))
        return result

    def retry(self, measurement_id, *, next_attempt):
        self._retry_due[measurement_id] = self._last_mono + min(
            60, max(0, next_attempt - self._last_wall)
        )
        with self.db:
            self._checkpoint_clock()
            self.db.execute(
                "UPDATE messages SET attempts=attempts+1,next_attempt=? WHERE measurement_id=?",
                (next_attempt, measurement_id),
            )

    def delivered(self, measurement_id, *, now=None, monotonic_now=None):
        now = self._tick(now, monotonic_now)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO receipts SELECT measurement_id,digest,? "
                "FROM messages WHERE measurement_id=?",
                (now, measurement_id),
            )
            self.db.execute("DELETE FROM messages WHERE measurement_id=?", (measurement_id,))
            self._retry_due.pop(measurement_id, None)
            self._checkpoint_clock()

    def quarantine(self, measurement_id, reason, *, now=None, monotonic_now=None):
        now = self._tick(now, monotonic_now)
        with self.db:
            row = self.db.execute(
                "SELECT measurement_id FROM messages WHERE measurement_id=?", (measurement_id,)
            ).fetchone()
            if row is None:
                return
            self.db.execute(
                "INSERT OR REPLACE INTO quarantine SELECT measurement_id,digest,?,? "
                "FROM messages WHERE measurement_id=?",
                (reason, now, measurement_id),
            )
            self._evict(row, reason)
            self._maintain(now)

    def stats(self):
        logical = self._tick()
        count, size = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(body)),0) FROM messages"
        ).fetchone()
        meta = dict(self.db.execute("SELECT key,value FROM metadata").fetchall())
        oldest = self.db.execute(
            "SELECT received_utc,created FROM messages ORDER BY ordinal LIMIT 1"
        ).fetchone()
        return {
            "pending": count,
            "payload_bytes": size,
            "quarantined": self.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
            **meta,
            "lost_records": int(meta.get("lost_records", 0)),
            "oldest_pending_unix": oldest[0] if oldest else None,
            "oldest_pending_age_seconds": max(0, logical - oldest[1]) if oldest else None,
            **{
                key: float(meta[key])
                for key in ("loss_range_start_unix", "loss_range_end_unix")
                if key in meta
            },
        }
