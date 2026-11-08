"""Single-sensor section geometry and filtering, with no cross-sensor point fusion."""

import math
from statistics import median


def geometry_matches(section, calibration):
    normal = [row[2] for row in calibration["rotation"]]
    horizontal = math.hypot(normal[0], normal[1])
    axis = section["axis_xy"]
    # A nearly horizontal scan plane cannot observe a vertical height profile.
    return horizontal >= 1e-6 and abs(normal[0] * axis[0] + normal[1] * axis[1]) <= 1e-8


def scan_profile(observation, calibration, section):
    filters = section["filters"]
    n = len(section["active_bins"])
    buckets = [[] for _ in range(n)]
    retained = 0
    ux, uy = section["axis_xy"]
    ox, oy = section["origin_xy_mm"]
    rotation, translation = calibration["rotation"], calibration["translation_mm"]
    for point in observation["scan"]["points"]:
        if (
            point["calculation_reason_codes"]
            or point["quality"] < filters["quality_min"]
            or not filters["distance_min_mm"] <= point["distance_mm"] <= filters["distance_max_mm"]
        ):
            continue
        theta = math.radians(
            (
                calibration["angle_sign"] * point["angle_mdeg"]
                + calibration["angle_zero_offset_mdeg"]
            )
            / 1000
        )
        lx, ly = point["distance_mm"] * math.cos(theta), point["distance_mm"] * math.sin(theta)
        x, y, z = [r[0] * lx + r[1] * ly + t for r, t in zip(rotation, translation, strict=True)]
        along = (x - ox) * ux + (y - oy) * uy
        i = math.floor(along / section["bin_width_mm"])
        if not 0 <= i < n or not section["active_bins"][i]:
            continue
        floor, ceiling = section["floor_z_mm"][i], section["ceiling_z_mm"][i]
        margin = filters["height_margin_mm"]
        if not floor - margin <= z <= ceiling + margin:
            continue
        buckets[i].append(z)
        retained += 1
    profile = [median(b) if b else None for b in buckets]
    cleaned, rejected = list(profile), 0
    radius = filters["spatial_radius_bins"]
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


def build_profile(configuration, section, calibration, observations):
    per_scan = [scan_profile(o, calibration, section) for o in observations]
    minimum = configuration["judgment"]["window"]["min_scans"]
    bins = []
    sid = section["sensor_id"]
    for i, active in enumerate(section["active_bins"]):
        values = [p[0][i] for p in per_scan if p[0][i] is not None]
        z = median(values) if active and len(values) >= minimum else None
        bins.append(
            {
                "index": i,
                "floor_z_mm": section["floor_z_mm"][i],
                "ceiling_z_mm": section["ceiling_z_mm"][i],
                "height_mm": None if z is None else z - section["floor_z_mm"][i],
                "source": "MASKED"
                if not active
                else "OBSERVED"
                if z is not None
                else "UNAVAILABLE",
                "sensor_ids": [sid] if z is not None else [],
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
            and i - start <= section["max_interpolation_gap_bins"]
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
        "coordinate_frame_id": configuration["coordinate_frame"]["frame_id"],
        "origin_xy_mm": list(section["origin_xy_mm"]),
        "axis_xy": list(section["axis_xy"]),
        "plane_origin_mm": list(calibration["translation_mm"]),
        "plane_normal": [row[2] for row in calibration["rotation"]],
        "area_basis": "VERTICAL_PROJECTION",
        "bin_width_mm": width,
        "bins": bins,
        "area_mm2": area,
        "capacity_area_mm2": capacity,
        "area_ratio": None if area is None else area / capacity,
        "coverage_ratio": sum(b["source"] == "OBSERVED" for b in active) / len(active),
        "sensor_stats": [
            {
                "sensor_id": sid,
                "scan_count": len(observations),
                "input_points": sum(len(o["scan"]["points"]) for o in observations),
                "retained_points": sum(p[1] for p in per_scan),
                "spatial_rejected_bins": sum(p[2] for p in per_scan),
            }
        ],
    }
