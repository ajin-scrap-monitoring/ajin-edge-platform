"""Opt-in point-cloud contract, independent of the legacy raw point representation."""

import math

LIMIT = 9007199254740991


def obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def arr(items, **limits):
    return {"type": "array", "items": items, **limits}


def uint(maximum=LIMIT, minimum=0):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


ID = {"type": "string", "minLength": 1, "maxLength": 128}
TIME = {"type": "string", "format": "date-time", "pattern": r"(Z|\+00:00)$"}
REASONS = arr(ID, uniqueItems=True)
SETTINGS_SCHEMA = obj(
    {
        "enabled": {"type": "boolean"},
        "processing_version": {"enum": ["ANGULAR_RANGE_MEDIAN_V1"]},
        "quality_min": uint(255),
        "distance_min_mm": uint((2**32 - 1) // 4),
        "distance_max_mm": uint((2**32 - 1) // 4, 1),
        "spatial_radius_points": uint(8),
        "spatial_min_neighbors": uint(16, 1),
        "spatial_max_deviation_mm": {"type": "number", "minimum": 0, "maximum": 1e9},
        "spatial_neighbor_spread_mm": {"type": "number", "minimum": 0, "maximum": 1e9},
        "max_adjacent_angle_gap_mdeg": uint(180000, 1),
        "angle_match_tolerance_mdeg": uint(1000),
    }
)
WINDOW_SCHEMA = obj(
    {
        "scan_count": uint(8, 1),
        "min_scans": uint(8, 1),
        "max_age_ms": uint(1000, 1),
        "pair_tolerance_ms": uint(100),
    }
)
REF_SCHEMA = obj(
    {
        "scan_id": ID,
        "sensor_id": ID,
        "config_revision": ID,
        "calibration_version": nullable(ID),
        "coordinate_frame_id": ID,
    }
)
ANCHOR_SCHEMA = obj(
    {
        "scan_id": ID,
        "stream_instance_id": ID,
        "sequence": uint(),
        "acquired_at": TIME,
        "timestamp_source": {"enum": ["EDGE_SCAN_RECEIVED"]},
        "clock_domain_id": ID,
        "acquired_monotonic_ns": {"type": "string", "pattern": "^[0-9]{1,20}$"},
        "clock_state": {"enum": ["SYNCED", "DEGRADED", "UNSYNCED"]},
        "clock_offset_ms": nullable({"type": "number"}),
        "source_point_count": uint(),
        "selected_point_count": uint(),
    }
)
INVALID_CODES = [
    "NO_SCAN",
    "SDK_INVALID_RANGE",
    "SOURCE_INVALID",
    "LOW_QUALITY",
    "OUT_OF_RANGE",
    "SPATIAL_OUTLIER",
    "INSUFFICIENT_HISTORY",
    "FILTER_UNVERIFIED",
    "CALIBRATION_MISSING",
    "CALIBRATION_UNVERIFIED",
    "CALIBRATION_MISMATCH",
    "COORDINATE_OUT_OF_RANGE",
    "INPUT_EXPIRED_DURING_FILTER",
]
VECTOR_FIELDS = (
    "point_indices",
    "angles_mdeg",
    "xyz_mm",
    "valid",
    "invalid_reason_codes",
    "sample_counts",
)
CLOUD_SCHEMA = obj(
    {
        "sensor_id": ID,
        "state": {"enum": ["AVAILABLE", "LIMITED", "UNAVAILABLE"]},
        "reason_codes": REASONS,
        "coordinate_frame_id": ID,
        "calibration_version": nullable(ID),
        "verification_state": {"enum": ["VERIFIED", "UNVERIFIED", "DEFAULT"]},
        "processing_version": {"enum": ["ANGULAR_RANGE_MEDIAN_V1"]},
        "anchor": nullable(ANCHOR_SCHEMA),
        "source_scans": arr(REF_SCHEMA, maxItems=8, uniqueItems=True),
        "point_count": uint(),
        "valid_point_count": uint(),
        "point_indices": arr(uint()),
        "angles_mdeg": arr(uint(359999)),
        "xyz_mm": arr(
            nullable(
                arr({"type": "number", "minimum": -LIMIT, "maximum": LIMIT}, minItems=3, maxItems=3)
            )
        ),
        "valid": arr({"type": "boolean"}),
        "invalid_reason_codes": arr(arr({"enum": INVALID_CODES}, uniqueItems=True, maxItems=1)),
        "sample_counts": arr(uint(8)),
        "processing": obj({**SETTINGS_SCHEMA["properties"], "window": WINDOW_SCHEMA}),
    }
)


def validate_settings(settings):
    if any(
        type(settings[key]) is not int
        for key, schema in SETTINGS_SCHEMA["properties"].items()
        if schema.get("type") == "integer"
    ):
        raise ValueError("point cloud integer settings must be JSON integers")
    if settings["distance_min_mm"] > settings["distance_max_mm"] or (
        settings["spatial_radius_points"]
        and settings["spatial_min_neighbors"] > 2 * settings["spatial_radius_points"]
    ):
        raise ValueError("inconsistent point cloud filter settings")


def structural_clouds(clouds):
    return [{**c, **{key: [] for key in VECTOR_FIELDS}} for c in clouds]


def _refkey(ref):
    return tuple(
        ref[k]
        for k in (
            "sensor_id",
            "scan_id",
            "config_revision",
            "calibration_version",
            "coordinate_frame_id",
        )
    )


def validate_clouds(payload):
    clouds = payload.get("filtered_point_clouds")
    if clouds is None:
        return
    if payload["assessment_type"] != "STATE" or payload.get("section_profiles") is None:
        raise ValueError("point clouds require independent STATE")
    ids = [c["sensor_id"] for c in clouds]
    if len(set(ids)) != 2 or set(ids) != {s["sensor_id"] for s in payload["section_profiles"]}:
        raise ValueError("point cloud sensor mismatch")
    references = {_refkey(r) for r in payload["evidence"]["scans"]}
    for c in clouds:
        n = c["point_count"]
        if any(not isinstance(c[key], list) or len(c[key]) != n for key in VECTOR_FIELDS):
            raise ValueError("point cloud vector lengths differ")
        validate_settings(c["processing"])
        if (
            not c["processing"]["enabled"]
            or c["processing_version"] != c["processing"]["processing_version"]
        ):
            raise ValueError("disabled or inconsistent cloud processing")
        window = c["processing"]["window"]
        sources = c["source_scans"]
        if window["min_scans"] > window["scan_count"] or len(sources) > window["scan_count"]:
            raise ValueError("invalid point cloud window")
        if any(
            _refkey(r) not in references
            or r["sensor_id"] != c["sensor_id"]
            or r["config_revision"] != payload["config_revision"]
            or r["coordinate_frame_id"] != c["coordinate_frame_id"]
            for r in sources
        ):
            raise ValueError("point cloud source evidence mismatch")
        if len({r["scan_id"] for r in sources}) != len(sources):
            raise ValueError("duplicate point cloud source")
        anchor = c["anchor"]
        if anchor is None:
            if n or sources:
                raise ValueError("missing point cloud anchor")
        elif (
            anchor["selected_point_count"] != n
            or anchor["source_point_count"] < n
            or not any(r["scan_id"] == anchor["scan_id"] for r in sources)
        ):
            raise ValueError("point cloud anchor mismatch")
        indices = c["point_indices"]
        if any(
            type(i) is not int or not 0 <= i < anchor["source_point_count"] for i in indices
        ) or indices != sorted(set(indices)):
            raise ValueError("invalid cloud point indices")
        count = 0
        for angle, xyz, valid, reasons, samples in zip(
            *(
                c[k]
                for k in ("angles_mdeg", "xyz_mm", "valid", "invalid_reason_codes", "sample_counts")
            ),
            strict=True,
        ):
            if type(angle) is not int or not 0 <= angle <= 359999 or type(valid) is not bool:
                raise ValueError("invalid cloud angle or validity")
            if type(samples) is not int or not 0 <= samples <= len(sources):
                raise ValueError("invalid cloud sample count")
            if (
                not isinstance(reasons, list)
                or len(reasons) > 1
                or any(r not in INVALID_CODES for r in reasons)
            ):
                raise ValueError("invalid cloud point reason")
            if valid:
                if (
                    reasons
                    or samples < window["min_scans"]
                    or not isinstance(xyz, list)
                    or len(xyz) != 3
                    or any(
                        type(v) not in (int, float) or not math.isfinite(v) or abs(v) > LIMIT
                        for v in xyz
                    )
                ):
                    raise ValueError("invalid filtered coordinate")
                count += 1
            elif xyz is not None or not reasons:
                raise ValueError("invalid cloud null/mask relationship")
        if count != c["valid_point_count"]:
            raise ValueError("point cloud valid count mismatch")
        expected = (
            "UNAVAILABLE"
            if not count
            else "AVAILABLE"
            if count == n and c["verification_state"] == "VERIFIED"
            else "LIMITED"
        )
        if c["state"] != expected or (
            count
            and (
                c["verification_state"] == "UNVERIFIED"
                or c["calibration_version"] is None
                or any(r["calibration_version"] != c["calibration_version"] for r in sources)
            )
        ):
            raise ValueError("point cloud state or calibration mismatch")
