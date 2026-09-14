"""Host clock probe: missing/unverifiable synchronization is UNSYNCED."""

import json
import math
import subprocess
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
