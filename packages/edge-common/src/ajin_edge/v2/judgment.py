"""Derived section profiles. Raw observations are never edited or replaced."""

import json
import math
from collections import deque
from copy import deepcopy
from itertools import product
from statistics import median
from threading import Lock

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .judgment_schema import JUDGMENT_SCHEMA


def validate_judgment(model):
    json.dumps(model, allow_nan=False)
    try:
        Draft202012Validator(JUDGMENT_SCHEMA).validate(model)
    except ValidationError as error:
        raise ValueError("invalid judgment settings") from error
    section, filters, window = model["section"], model["filters"], model["window"]
    integer_values = [
        section["bin_width_mm"],
        model["max_interpolation_gap_bins"],
        *window.values(),
        *(
            filters[k]
            for k in (
                "quality_min",
                "distance_min_mm",
                "distance_max_mm",
                "height_margin_mm",
                "spatial_radius_bins",
            )
        ),
    ]
    if any(type(value) is not int for value in integer_values):
        raise ValueError("judgment integer parameters require JSON integer values")
    n = len(section["floor_z_mm"])
    if (
        len(section["ceiling_z_mm"]) != n
        or len(section["active_bins"]) != n
        or not any(section["active_bins"])
        or any(b <= a for a, b in zip(section["floor_z_mm"], section["ceiling_z_mm"], strict=True))
        or not math.isclose(sum(x * x for x in section["axis_xy"]), 1, abs_tol=1e-8)
        or filters["distance_min_mm"] > filters["distance_max_mm"]
        or window["min_scans"] > window["scan_count"]
    ):
        raise ValueError("inconsistent section geometry or window")
    curve = model["fill_calibration"]
    if curve is not None and (
        curve[0][0] != 0
        or curve[-1][0] != 1
        or any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(curve, curve[1:], strict=False))
    ):
        raise ValueError("fill calibration must be monotone with input endpoints 0 and 1")


def mapped_fill(ratio, curve):
    for left, right in zip(curve, curve[1:], strict=False):
        if ratio <= right[0]:
            return left[1] + (ratio - left[0]) * (right[1] - left[1]) / (right[0] - left[0])
    return curve[-1][1]


class SectionJudgment:
    def __init__(self, configuration):
        self.config = deepcopy(configuration)
        self.model = self.config.get("judgment")
        self.lock = Lock()
        count = self.model["window"]["scan_count"] if self.model else 1
        self.history = {s["sensor_id"]: deque(maxlen=count) for s in self.config["sensors"]}

    def ingest(self, observation):
        if not self.model:
            return
        scan = observation["scan"]
        with self.lock:
            history = self.history[observation["sensor_id"]]
            if history:
                previous = history[-1]["scan"]
                if (
                    any(previous[k] != scan[k] for k in ("stream_instance_id", "clock_domain_id"))
                    or int(scan["acquired_monotonic_ns"]) - int(previous["acquired_monotonic_ns"])
                    > self.model["window"]["max_age_ms"] * 1000000
                ):
                    history.clear()
            history.append(deepcopy(observation))

    def calculate(self, now_ns):
        """Return profile, reasons, actual input observations; all storage is bounded."""
        if not self.model:
            return None, ["FILL_METHOD_NOT_CONFIGURED"], []
        if self.model["verification_state"] != "VERIFIED":
            return None, ["FILL_METHOD_NOT_VALIDATED"], []
        sensors = {s["sensor_id"]: s for s in self.config["sensors"]}
        if any(s["calibration"] is None for s in sensors.values()):
            return None, ["CALIBRATION_MISSING"], []
        if any(s["calibration"]["verification_state"] != "VERIFIED" for s in sensors.values()):
            return None, ["CALIBRATION_UNVERIFIED"], []
        window = self.model["window"]
        with self.lock:
            histories = {
                sid: [
                    o
                    for o in h
                    if 0
                    <= now_ns - int(o["scan"]["acquired_monotonic_ns"])
                    <= window["max_age_ms"] * 1000000
                ]
                for sid, h in self.history.items()
            }
        if any(not h for h in histories.values()):
            return None, ["INSUFFICIENT_INPUT"], []
        values = list(histories.values())
        pairs = [
            (a, b)
            for a, b in product(*values)
            if a["scan"]["clock_domain_id"] == b["scan"]["clock_domain_id"]
            and abs(
                int(a["scan"]["acquired_monotonic_ns"]) - int(b["scan"]["acquired_monotonic_ns"])
            )
            <= window["pair_tolerance_ms"] * 1000000
        ]
        if not pairs:
            return None, ["SENSOR_TIME_MISMATCH"], []
        pair = max(
            pairs,
            key=lambda p: (
                min(int(o["scan"]["acquired_monotonic_ns"]) for o in p),
                -abs(
                    int(p[0]["scan"]["acquired_monotonic_ns"])
                    - int(p[1]["scan"]["acquired_monotonic_ns"])
                ),
            ),
        )
        chosen = {}
        for sid, anchor in zip(histories, pair, strict=True):
            limit = int(anchor["scan"]["acquired_monotonic_ns"])
            chosen[sid] = [
                o for o in histories[sid] if int(o["scan"]["acquired_monotonic_ns"]) <= limit
            ]
        if any(len(h) < window["min_scans"] for h in chosen.values()):
            return None, ["INSUFFICIENT_HISTORY"], []
        used = [o for h in chosen.values() for o in h]
        if any(
            o["transform_state"] != "VERIFIED"
            or o["calibration_version"] != sensors[o["sensor_id"]]["calibration"]["version"]
            for o in used
        ):
            return None, ["CALIBRATION_MISMATCH"], []
        profiles, stats = {}, []
        for sid, observations in chosen.items():
            per_scan = [self._scan_profile(o, sensors[sid]["calibration"]) for o in observations]
            n = len(self.model["section"]["active_bins"])
            profiles[sid] = []
            for i in range(n):
                heights = [p[0][i] for p in per_scan if p[0][i] is not None]
                profiles[sid].append(
                    median(heights) if len(heights) >= window["min_scans"] else None
                )
            stats.append(
                {
                    "sensor_id": sid,
                    "scan_count": len(observations),
                    "input_points": sum(len(o["scan"]["points"]) for o in observations),
                    "retained_points": sum(p[1] for p in per_scan),
                    "spatial_rejected_bins": sum(p[2] for p in per_scan),
                }
            )
        profile = self._fuse(profiles, stats)
        reasons = []
        if any(b["source"] == "CONFLICT" for b in profile["bins"]):
            reasons.append("SENSOR_DISAGREEMENT")
        if profile["area_ratio"] is None:
            reasons.append("SECTION_INCOMPLETE")
        elif any(b["source"] == "INTERPOLATED" for b in profile["bins"]):
            reasons.append("INTERPOLATED_BINS")
        if self.model["fill_calibration"] is None:
            reasons.append("FILL_CALIBRATION_MISSING")
        return profile, reasons, used

    def _scan_profile(self, observation, cal):
        section, filters = self.model["section"], self.model["filters"]
        n = len(section["active_bins"])
        buckets = [[] for _ in range(n)]
        retained = 0
        ux, uy = section["axis_xy"]
        ox, oy = section["origin_xy_mm"]
        r, t = cal["rotation"], cal["translation_mm"]
        for point in observation["scan"]["points"]:
            if (
                point["calculation_reason_codes"]
                or point["quality"] < filters["quality_min"]
                or not filters["distance_min_mm"]
                <= point["distance_mm"]
                <= filters["distance_max_mm"]
            ):
                continue
            theta = math.radians(
                (cal["angle_sign"] * point["angle_mdeg"] + cal["angle_zero_offset_mdeg"]) / 1000
            )
            lx, ly = point["distance_mm"] * math.cos(theta), point["distance_mm"] * math.sin(theta)
            x, y, z = [row[0] * lx + row[1] * ly + shift for row, shift in zip(r, t, strict=True)]
            along = (x - ox) * ux + (y - oy) * uy
            across = -(x - ox) * uy + (y - oy) * ux
            i = math.floor(along / section["bin_width_mm"])
            if (
                not 0 <= i < n
                or not section["active_bins"][i]
                or abs(across) > section["plane_tolerance_mm"]
            ):
                continue
            floor, ceiling = section["floor_z_mm"][i], section["ceiling_z_mm"][i]
            margin = filters["height_margin_mm"]
            if not floor - margin <= z <= ceiling + margin:
                continue
            buckets[i].append(z)
            retained += 1
        profile = [median(b) if b else None for b in buckets]
        radius, rejected = filters["spatial_radius_bins"], 0
        cleaned = list(profile)
        if radius:
            for i, value in enumerate(profile):
                left, right = max(0, i - radius), min(n, i + radius + 1)
                neighbors = [x for x in profile[left:right] if x is not None]
                if (
                    value is not None
                    and all(section["active_bins"][left:right])
                    and len(neighbors) >= 3
                    and abs(value - median(neighbors)) > filters["spatial_max_deviation_mm"]
                ):
                    cleaned[i], rejected = None, rejected + 1
        return cleaned, retained, rejected

    def _fuse(self, profiles, stats):
        section = self.model["section"]
        bins = []
        for i, active in enumerate(section["active_bins"]):
            values = [(sid, p[i]) for sid, p in profiles.items() if p[i] is not None]
            source = "MASKED" if not active else "UNAVAILABLE"
            z = None
            if active and values:
                if (
                    len(values) == 2
                    and abs(values[0][1] - values[1][1]) > self.model["max_sensor_disagreement_mm"]
                ):
                    source = "CONFLICT"
                else:
                    source, z = "OBSERVED", median(v for _, v in values)
            bins.append(
                {
                    "index": i,
                    "floor_z_mm": section["floor_z_mm"][i],
                    "ceiling_z_mm": section["ceiling_z_mm"][i],
                    "height_mm": None if z is None else z - section["floor_z_mm"][i],
                    "source": source,
                    "sensor_ids": [sid for sid, _ in values] if active else [],
                }
            )
        i = 0
        while i < len(bins):
            if bins[i]["source"] != "UNAVAILABLE":
                i += 1
                continue
            start = i
            while i < len(bins) and bins[i]["source"] == "UNAVAILABLE":
                i += 1
            if (
                start > 0
                and i < len(bins)
                and i - start <= self.model["max_interpolation_gap_bins"]
                and bins[start - 1]["source"] == bins[i]["source"] == "OBSERVED"
            ):
                left = bins[start - 1]["height_mm"] + bins[start - 1]["floor_z_mm"]
                right = bins[i]["height_mm"] + bins[i]["floor_z_mm"]
                for j in range(start, i):
                    z = left + (right - left) * (j - start + 1) / (i - start + 1)
                    bins[j].update(height_mm=z - bins[j]["floor_z_mm"], source="INTERPOLATED")
        for b in bins:
            if b["height_mm"] is not None:
                b["height_mm"] = max(0.0, min(b["ceiling_z_mm"] - b["floor_z_mm"], b["height_mm"]))
        active = [b for b in bins if b["source"] != "MASKED"]
        width = section["bin_width_mm"]
        capacity = sum(width * (b["ceiling_z_mm"] - b["floor_z_mm"]) for b in active)
        area = (
            sum(width * b["height_mm"] for b in active)
            if all(b["height_mm"] is not None for b in active)
            else None
        )
        return {
            "coordinate_frame_id": self.config["coordinate_frame"]["frame_id"],
            "origin_xy_mm": list(section["origin_xy_mm"]),
            "axis_xy": list(section["axis_xy"]),
            "bin_width_mm": width,
            "bins": bins,
            "area_mm2": area,
            "capacity_area_mm2": capacity,
            "area_ratio": None if area is None else area / capacity,
            "coverage_ratio": sum(b["source"] == "OBSERVED" for b in active) / len(active),
            "sensor_stats": stats,
        }
