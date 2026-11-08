"""Bounded, optional wire extensions for reproducible section judgments."""


def obj(fields):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


def arr(items, low=0, high=512):
    return {"type": "array", "items": items, "minItems": low, "maxItems": high}


def num(low, high):
    return {"type": "number", "minimum": low, "maximum": high}


def uint(low=0, high=9007199254740991):
    return {"type": "integer", "minimum": low, "maximum": high}


def optional(value):
    return {"anyOf": [value, {"type": "null"}]}


ID = {"type": "string", "minLength": 1, "maxLength": 128}
JUDGMENT_SCHEMA = obj(
    {
        "method_version": ID,
        "verification_state": {"enum": ["VERIFIED", "UNVERIFIED"]},
        "section": obj(
            {
                "origin_xy_mm": arr(num(-1e9, 1e9), 2, 2),
                "axis_xy": arr(num(-1, 1), 2, 2),
                "plane_tolerance_mm": num(0, 1e6),
                "bin_width_mm": uint(1, 1000000),
                "floor_z_mm": arr(num(-1e9, 1e9), 1),
                "ceiling_z_mm": arr(num(-1e9, 1e9), 1),
                "active_bins": arr({"type": "boolean"}, 1),
            }
        ),
        "filters": obj(
            {
                "quality_min": uint(0, 255),
                "distance_min_mm": uint(1, (2**32 - 1) // 4),
                "distance_max_mm": uint(1, (2**32 - 1) // 4),
                "height_margin_mm": uint(0, 1000000),
                "spatial_radius_bins": uint(0, 5),
                "spatial_max_deviation_mm": num(0, 1e9),
            }
        ),
        "window": obj(
            {
                "scan_count": uint(1, 8),
                "min_scans": uint(1, 8),
                "max_age_ms": uint(1, 1000),
                "pair_tolerance_ms": uint(0, 100),
            }
        ),
        "max_interpolation_gap_bins": uint(0, 10),
        "max_sensor_disagreement_mm": num(0, 1e9),
        "fill_calibration": optional(arr(arr(num(0, 1), 2, 2), 2, 128)),
    }
)

SECTION_PROFILE_SCHEMA = obj(
    {
        "coordinate_frame_id": ID,
        "origin_xy_mm": arr(num(-1e9, 1e9), 2, 2),
        "axis_xy": arr(num(-1, 1), 2, 2),
        "bin_width_mm": uint(1, 1000000),
        "bins": arr(
            obj(
                {
                    "index": uint(0, 511),
                    "floor_z_mm": num(-1e9, 1e9),
                    "ceiling_z_mm": num(-1e9, 1e9),
                    "height_mm": optional(num(0, 2e9)),
                    "source": {
                        "enum": ["OBSERVED", "INTERPOLATED", "UNAVAILABLE", "MASKED", "CONFLICT"]
                    },
                    "sensor_ids": arr(ID, 0, 2),
                }
            ),
            1,
        ),
        "area_mm2": optional(num(0, 1e21)),
        "capacity_area_mm2": {"type": "number", "exclusiveMinimum": 0, "maximum": 1e21},
        "area_ratio": optional(num(0, 1)),
        "coverage_ratio": num(0, 1),
        "sensor_stats": arr(
            obj(
                {
                    "sensor_id": ID,
                    "scan_count": uint(1, 8),
                    "input_points": uint(),
                    "retained_points": uint(),
                    "spatial_rejected_bins": uint(),
                }
            ),
            2,
            2,
        ),
    }
)
