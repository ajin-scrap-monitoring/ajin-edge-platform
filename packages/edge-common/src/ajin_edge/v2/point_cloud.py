"""Independent full selected-scan filtering. Input observations remain immutable."""

import math
from bisect import bisect_left
from copy import deepcopy
from statistics import median

DEFAULT_POINT_CLOUD = {
    "enabled": True,
    "processing_version": "ANGULAR_RANGE_MEDIAN_V1",
    "quality_min": 10,
    "distance_min_mm": 1,
    "distance_max_mm": 30000,
    "spatial_radius_points": 2,
    "spatial_min_neighbors": 3,
    "spatial_max_deviation_mm": 200,
    "spatial_neighbor_spread_mm": 100,
    "max_adjacent_angle_gap_mdeg": 500,
    "angle_match_tolerance_mdeg": 50,
}


def enabled(model):
    return bool(model and model.get("point_cloud", {}).get("enabled"))


def reference(configuration, observation):
    return {
        "sensor_id": observation["sensor_id"],
        "scan_id": observation["scan"]["scan_id"],
        "config_revision": configuration["config_revision"],
        "calibration_version": observation["calibration_version"],
        "coordinate_frame_id": configuration["coordinate_frame"]["frame_id"],
    }


def _interval(angle, intervals):
    return next(
        (i for i, r in enumerate(intervals) if r["start_inclusive"] <= angle < r["end_exclusive"]),
        None,
    )


def _clean(points, intervals, settings):
    reasons = []
    groups = {}
    for i, point in enumerate(points):
        interval = _interval(point["angle_mdeg"], intervals)
        code = (
            "SDK_INVALID_RANGE"
            if "SDK_INVALID_RANGE" in point["calculation_reason_codes"]
            else "SOURCE_INVALID"
            if point["calculation_reason_codes"] or interval is None
            else "LOW_QUALITY"
            if point["quality"] < settings["quality_min"]
            else "OUT_OF_RANGE"
            if not settings["distance_min_mm"]
            <= point["distance_mm"]
            <= settings["distance_max_mm"]
            else None
        )
        reasons.append(code)
        if interval is not None:
            groups.setdefault(interval, []).append(i)
    original = list(reasons)
    radius = settings["spatial_radius_points"]
    if not radius:
        return reasons
    for indices in groups.values():
        indices.sort(key=lambda i: (points[i]["angle_mdeg"], points[i]["index"]))
        for k, i in enumerate(indices):
            if original[i] is not None:
                continue
            neighbors = []
            for direction in (-1, 1):
                previous = k
                for distance in range(1, radius + 1):
                    j = k + direction * distance
                    if not 0 <= j < len(indices):
                        break
                    at, prev = indices[j], indices[previous]
                    if (
                        abs(points[at]["angle_mdeg"] - points[prev]["angle_mdeg"])
                        > settings["max_adjacent_angle_gap_mdeg"]
                    ):
                        break
                    if original[at] is None:
                        neighbors.append(points[at]["distance_mm"])
                    previous = j
            if (
                len(neighbors) >= settings["spatial_min_neighbors"]
                and max(neighbors) - min(neighbors) <= settings["spatial_neighbor_spread_mm"]
                and abs(points[i]["distance_mm"] - median(neighbors))
                > settings["spatial_max_deviation_mm"]
            ):
                reasons[i] = "SPATIAL_OUTLIER"
    return reasons


def _match(anchor, points, intervals, tolerance):
    groups = {}
    for i, point in enumerate(points):
        key = _interval(point["angle_mdeg"], intervals)
        if key is not None:
            groups.setdefault(key, []).append((point["angle_mdeg"], point["index"], i))
    for key, group in groups.items():
        group.sort()
        # Equal angles use the original lowest index, including a tie approached
        # from above the angle by bisect. Losing points are not alternative rays.
        unique = {}
        for row in group:
            unique.setdefault(row[0], row)
        groups[key] = list(unique.values())
    candidates = []
    for i, point in enumerate(anchor):
        group = groups.get(_interval(point["angle_mdeg"], intervals), [])
        at = bisect_left(group, (point["angle_mdeg"], -1, -1))
        choices = group[max(0, at - 1) : at + 1]
        if not choices:
            continue
        a, original_index, j = min(
            choices, key=lambda row: (abs(row[0] - point["angle_mdeg"]), row[1])
        )
        delta = abs(a - point["angle_mdeg"])
        if delta <= tolerance:
            candidates.append((delta, i, original_index, j))
    result, used = {}, set()
    for _, i, _, j in sorted(candidates):
        if j not in used:
            result[i] = j
            used.add(j)
    return result


def _finish(result):
    count = sum(result["valid"])
    result["valid_point_count"] = count
    result["state"] = (
        "UNAVAILABLE"
        if not count
        else "AVAILABLE"
        if count == result["point_count"] and result["verification_state"] == "VERIFIED"
        else "LIMITED"
    )
    reasons = set(result["reason_codes"])
    if result["verification_state"] == "DEFAULT":
        reasons.add("DEFAULT_SETTINGS")
    if result["point_count"] and not count:
        reasons.add("NO_VALID_POINTS")
    elif not result["point_count"] and result["anchor"] is not None:
        reasons.add("NO_SELECTED_POINTS")
    elif count < result["point_count"]:
        reasons.add("PARTIAL_POINTS")
    result["reason_codes"] = sorted(reasons)
    return result


def invalidate_expired(result):
    result["xyz_mm"] = [None] * result["point_count"]
    result["valid"] = [False] * result["point_count"]
    result["invalid_reason_codes"] = [["INPUT_EXPIRED_DURING_FILTER"] for _ in result["xyz_mm"]]
    result["sample_counts"] = [0] * result["point_count"]
    result["reason_codes"] = ["INPUT_EXPIRED_DURING_FILTER"]
    return _finish(result)


def build_cloud(configuration, sensor, observations, *, now_ns):
    model = configuration["judgment"]
    settings, window = model["point_cloud"], model["window"]
    fresh = [
        o
        for o in observations
        if o["sensor_id"] == sensor["sensor_id"]
        and o["scan"] is not None
        and 0 <= now_ns - int(o["scan"]["acquired_monotonic_ns"]) <= window["max_age_ms"] * 1000000
    ]
    fresh = list({o["scan"]["scan_id"]: o for o in fresh}.values())
    fresh.sort(key=lambda o: int(o["scan"]["acquired_monotonic_ns"]))
    if fresh:
        latest = fresh[-1]["scan"]
        fresh = [
            o
            for o in fresh
            if all(o["scan"][k] == latest[k] for k in ("stream_instance_id", "clock_domain_id"))
        ][-window["scan_count"] :]
    anchor = fresh[-1] if fresh else None
    points = anchor["scan"]["points"] if anchor else []
    n = len(points)
    cal = sensor["calibration"]
    result = {
        "sensor_id": sensor["sensor_id"],
        "state": "UNAVAILABLE",
        "reason_codes": [],
        "coordinate_frame_id": configuration["coordinate_frame"]["frame_id"],
        "calibration_version": cal["version"] if cal else None,
        "verification_state": model["verification_state"],
        "processing_version": settings["processing_version"],
        "anchor": {k: deepcopy(v) for k, v in anchor["scan"].items() if k != "points"}
        if anchor
        else None,
        "source_scans": [reference(configuration, o) for o in fresh],
        "point_count": n,
        "valid_point_count": 0,
        "point_indices": [p["index"] for p in points],
        "angles_mdeg": [p["angle_mdeg"] for p in points],
        "xyz_mm": [None] * n,
        "valid": [False] * n,
        "invalid_reason_codes": [[] for _ in points],
        "sample_counts": [0] * n,
        "processing": {**deepcopy(settings), "window": deepcopy(window)},
    }
    failure = (
        "NO_SCAN"
        if not anchor
        else "FILTER_UNVERIFIED"
        if model["verification_state"] == "UNVERIFIED"
        else "CALIBRATION_MISSING"
        if cal is None
        else "CALIBRATION_UNVERIFIED"
        if cal["verification_state"] != "VERIFIED"
        else "CALIBRATION_MISMATCH"
        if any(
            o["calibration_version"] != cal["version"] or o["transform_state"] != "VERIFIED"
            for o in fresh
        )
        else None
    )
    if failure:
        result["reason_codes"] = [failure]
        result["invalid_reason_codes"] = [[failure] for _ in points]
        return _finish(result)
    cleaned = [_clean(o["scan"]["points"], sensor["angle_intervals_mdeg"], settings) for o in fresh]
    mappings = [
        _match(
            points,
            o["scan"]["points"],
            sensor["angle_intervals_mdeg"],
            settings["angle_match_tolerance_mdeg"],
        )
        for o in fresh[:-1]
    ] + [{i: i for i in range(n)}]
    for i, point in enumerate(points):
        if cleaned[-1][i] is not None:
            result["invalid_reason_codes"][i] = [cleaned[-1][i]]
            continue
        values = [
            o["scan"]["points"][mapping[i]]["distance_mm"]
            for o, codes, mapping in zip(fresh, cleaned, mappings, strict=True)
            if i in mapping and codes[mapping[i]] is None
        ]
        result["sample_counts"][i] = len(values)
        if len(values) < window["min_scans"]:
            result["invalid_reason_codes"][i] = ["INSUFFICIENT_HISTORY"]
            continue
        angle = math.radians(
            (cal["angle_sign"] * point["angle_mdeg"] + cal["angle_zero_offset_mdeg"]) / 1000
        )
        distance = median(values)
        lx, ly = distance * math.cos(angle), distance * math.sin(angle)
        xyz = [
            round(r[0] * lx + r[1] * ly + t, 3)
            for r, t in zip(cal["rotation"], cal["translation_mm"], strict=True)
        ]
        if not all(math.isfinite(v) and abs(v) <= 9007199254740991 for v in xyz):
            result["invalid_reason_codes"][i] = ["COORDINATE_OUT_OF_RANGE"]
            continue
        result["xyz_mm"][i], result["valid"][i] = xyz, True
    return _finish(result)
