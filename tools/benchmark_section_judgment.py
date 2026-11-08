"""Synthetic performance probe; never loads or changes a device's runtime configuration."""

import argparse
import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.processing import transform_scan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error("iterations must be 1..100")
    path = Path(__file__).resolve().parents[1] / "examples/v2/judgment-configuration.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # These are explicit synthetic benchmark assertions, not installation validation.
    cfg["judgment"]["verification_state"] = "VERIFIED"
    for sensor in cfg["sensors"]:
        sensor["calibration"]["verification_state"] = "VERIFIED"
    cfg["judgment"]["fusion"]["verification_state"] = "VERIFIED"
    samples_by_sensor = {}
    for sensor, section in zip(cfg["sensors"], cfg["judgment"]["sections"], strict=True):
        section.update(
            origin_xy_mm=[0, 0],
            bin_width_mm=50,
            floor_z_mm=[0] * 128,
            ceiling_z_mm=[10000] * 128,
            active_bins=[True] * 128,
        )
        section["filters"]["spatial_radius_bins"] = 1
        cal = sensor["calibration"]
        normal = [row[2] for row in cal["rotation"]]
        norm = math.hypot(*normal[:2])
        samples = []
        for i in range(800):
            point = [v * (25 + i * 8) for v in section["axis_xy"]] + [5000]
            offset = (
                sum(
                    n * (o - p)
                    for n, o, p in zip(normal, cal["translation_mm"], point, strict=True)
                )
                / norm
            )
            point[0] += offset * normal[0] / norm
            point[1] += offset * normal[1] / norm
            delta = [p - o for p, o in zip(point, cal["translation_mm"], strict=True)]
            local = [sum(cal["rotation"][j][k] * delta[j] for j in range(3)) for k in range(2)]
            samples.append(
                {
                    "angle_mdeg": round(math.degrees(math.atan2(local[1], local[0])) * 1000)
                    % 360000,
                    "distance_mm": round(math.hypot(*local)),
                    "quality": 160,
                }
            )
        samples_by_sensor[sensor["sensor_id"]] = samples
    orch = Orchestrator(cfg)
    for sequence, ms in enumerate([600, 700, 800, 900, 1000], 1):
        for sensor in cfg["sensors"]:
            sid = sensor["sensor_id"]
            raw = {
                "sensor_id": sid,
                "scan_id": f"{sid}:{sequence}",
                "stream_instance_id": "synthetic-benchmark",
                "sequence": sequence,
                "acquired_at": (
                    datetime(2026, 11, 8, tzinfo=UTC) + timedelta(milliseconds=ms)
                ).isoformat(),
                "timestamp_source": "EDGE_SCAN_RECEIVED",
                "clock_domain_id": "synthetic-boot",
                "acquired_monotonic_ns": str(ms * 1000000),
                "clock_state": "UNSYNCED",
                "clock_offset_ms": None,
                "samples": samples_by_sensor[sid],
            }
            orch.ingest(transform_scan(raw, sensor), now_ns=ms * 1000000)
    timings = []
    for _ in range(args.iterations):
        started = time.perf_counter()
        bundle = orch.assessment_bundle(now_ns=1000000000)
        timings.append((time.perf_counter() - started) * 1000)
        assert len(bundle) == 11
        assert abs(bundle[-1]["fill_estimate"]["ratio"] - 0.5) < 0.001
        assert len(bundle[-1]["section_profiles"]) == 2
        assert all(len(body["sensors"][0]["scan"]["points"]) == 800 for body in bundle[:-1])
    report = {
        "points_per_sensor_scan": 800,
        "input_scans": 10,
        "bins_per_sensor_section": 128,
        "iterations": args.iterations,
        "bundle_messages": 11,
        "median_ms": round(median(timings), 3),
        "max_ms": round(max(timings), 3),
    }
    print(json.dumps(report))
    if max(timings) >= 1000:
        raise SystemExit("judgment exceeded one-second computation budget")


if __name__ == "__main__":
    main()
