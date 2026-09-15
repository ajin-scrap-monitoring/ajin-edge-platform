"""Host clock probe: missing/unverifiable synchronization is UNSYNCED."""

import json
import math
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def classify_clock(offset_ms):
    if offset_ms is None or not isinstance(offset_ms, (int, float)) or not math.isfinite(offset_ms):
        return {"state": "UNSYNCED", "offset_ms": None}
    error = abs(offset_ms)
    return {
        "state": "SYNCED" if error <= 100 else "DEGRADED" if error <= 500 else "UNSYNCED",
        "offset_ms": offset_ms,
    }


def read_clock(path):
    try:
        if Path(path).stat().st_size > 4096:
            return classify_clock(None)
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(value["reported_at"])).total_seconds()
        if not -1 <= age <= 30 or value.get("synchronized") is not True:
            return classify_clock(None)
        return classify_clock(value.get("offset_ms"))
    except (OSError, ValueError, KeyError, TypeError):
        return classify_clock(None)


def probe_chrony():
    """CSV: ref-id, name/IP, stratum, ref-time, system-time, ... leap (chrony 4.6)."""
    try:
        result = subprocess.run(
            ["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=2, check=True
        )
        fields = result.stdout.strip().split(",")
        if (
            len(fields) != 14
            or fields[-1].strip() != "Normal"
            or int(fields[2]) == 0
            or fields[0].upper() == "7F7F0101"
        ):
            return classify_clock(None)
        return classify_clock(float(fields[4]) * 1000)
    except (OSError, ValueError, subprocess.SubprocessError):
        return classify_clock(None)


def probe_timesyncd():
    """Read the last accepted NTP exchange; never infer zero offset from a flag.

    Offset is the last measured correction, not a continuous error bound.
    See systemd timesyncd-bus.c NTPMessage and timedatectl.c offset calculation.
    """

    def command(args):
        return subprocess.run(
            args, capture_output=True, text=True, timeout=2, check=True
        ).stdout.strip()

    def property_value(name, signature):
        value = json.loads(
            command(
                [
                    "busctl",
                    "--json=short",
                    "get-property",
                    "org.freedesktop.timesync1",
                    "/org/freedesktop/timesync1",
                    "org.freedesktop.timesync1.Manager",
                    name,
                ]
            )
        )
        if value["type"] != signature:
            raise ValueError("unexpected D-Bus signature")
        return value["data"]

    try:
        if command(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) != "yes":
            return classify_clock(None)
        data = property_value("NTPMessage", "(uuuuittayttttbtt)")
        poll_us = property_value("PollIntervalUSec", "t")
        if (
            not isinstance(data, list)
            or len(data) != 15
            or any(type(data[i]) is not int for i in (*range(7), 8, 9, 10, 11, 13, 14))
            or data[0] not in (0, 1, 2)
            or data[1] not in (3, 4)
            or data[2] != 4
            or not 1 <= data[3] <= 15
            or data[12] is not False
            or data[13] <= 0
            or type(poll_us) is not int
            or not 0 < poll_us <= 3_600_000_000
        ):
            return classify_clock(None)
        t1, t2, t3, t4 = data[8:12]
        if not all(0 < t < 2**63 for t in (t1, t2, t3, t4)) or t3 < t2 or t4 < t1:
            return classify_clock(None)
        age = (time.time_ns() // 1000 - t4) / 1_000_000
        max_age = min(3600, max(60, 2 * poll_us / 1_000_000))
        if not -1 <= age <= max_age:
            return classify_clock(None)
        return classify_clock(((t2 - t1) + (t3 - t4)) / 2000)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return classify_clock(None)
