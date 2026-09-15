"""Host exporter must use measured offsets and reject unverifiable NTP samples."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "case,expected",
    [
        ("valid", "SYNCED"),
        ("negative", "DEGRADED"),
        ("large", "UNSYNCED"),
        ("unsynchronized", "UNSYNCED"),
        ("stale", "UNSYNCED"),
        ("future", "UNSYNCED"),
        ("spike", "UNSYNCED"),
        ("leap", "UNSYNCED"),
        ("stratum", "UNSYNCED"),
        ("malformed", "UNSYNCED"),
        ("missing", "UNSYNCED"),
        ("bad_poll", "UNSYNCED"),
    ],
)
def test_timesyncd_exporter(monkeypatch, tmp_path, case, expected):
    t1 = 1_800_000_000_000_000
    # Four timestamps imply +25 ms offset and 20 ms round-trip delay.
    data = [
        0,
        4,
        4,
        2,
        -20,
        1000,
        1000,
        [0, 0, 0, 0],
        t1,
        t1 + 35_000,
        t1 + 36_000,
        t1 + 21_000,
        False,
        7,
        500,
    ]
    if case in ("negative", "large"):
        delta = -225_000 if case == "negative" else 975_000
        data[9] += delta
        data[10] += delta
    if case == "spike":
        data[12] = True
    if case == "leap":
        data[0] = 3
    if case == "stratum":
        data[3] = 0
    now = t1 + (200_000_000 if case == "stale" else -5_000_000 if case == "future" else 1_000_000)
    monkeypatch.setattr("time.time_ns", lambda: now * 1000)

    def run(command, **kwargs):
        if case == "missing":
            raise FileNotFoundError("host utility absent")
        if command[0] == "timedatectl":
            value = "no" if case == "unsynchronized" else "yes"
        elif command[-1] == "NTPMessage":
            value = (
                "{}"
                if case == "malformed"
                else json.dumps({"type": "(uuuuittayttttbtt)", "data": data})
            )
        elif command[-1] == "PollIntervalUSec":
            value = json.dumps({"type": "t", "data": -1 if case == "bad_poll" else 32_000_000})
        else:
            raise AssertionError(command)
        return SimpleNamespace(stdout=value)

    monkeypatch.setattr(subprocess, "run", run)
    output = tmp_path / "clock.json"
    exporter = Path(__file__).resolve().parents[1] / "tools/export_clock.py"
    monkeypatch.setattr(
        sys, "argv", [str(exporter), "--source", "timesyncd", "--once", "--output", str(output)]
    )
    runpy.run_path(str(exporter), run_name="__main__")
    result = json.loads(output.read_text())
    assert result["state"] == expected
    assert result["source"] == "timesyncd"
    assert result["offset_ms"] == {"valid": 25, "negative": -200, "large": 1000}.get(case)
    assert result["synchronized"] is (case in ("valid", "negative", "large"))
