"""Executable schemas exported for backend teams by tools/export_schemas.py."""

import json
import math
from datetime import UTC, datetime

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

TEXT = {"type": "string", "minLength": 1, "maxLength": 128}
RATIO = {"type": "number", "minimum": 0, "maximum": 1}
REASONS = {"type": "array", "items": TEXT, "maxItems": 64, "uniqueItems": True}
QUALITY = {
    "type": "object",
    "required": ["state", "confidence", "reason_codes"],
    "properties": {
        "state": {"enum": ["GOOD", "DEGRADED", "INVALID"]},
        "confidence": RATIO,
        "reason_codes": REASONS,
    },
    "additionalProperties": False,
}
SENSOR = {
    "type": "object",
    "required": ["sensor_id", "sequence", "valid_sample_ratio", "coverage_ratio", "state"],
    "properties": {
        "sensor_id": TEXT,
        "sequence": {"type": "integer", "minimum": 0},
        "instance_id": TEXT,
        "section_fill_ratio": RATIO,
        "valid_sample_ratio": RATIO,
        "coverage_ratio": RATIO,
        "median_height_mm": {"type": "number"},
        "p90_height_mm": {"type": "number"},
        "state": {"enum": ["GOOD", "DEGRADED", "INVALID"]},
    },
}
MEASUREMENT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:ajin:edge:measurement:1.0",
    "type": "object",
    "required": [
        "schema_version",
        "measurement_id",
        "measurement_cycle_id",
        "site_id",
        "edge_id",
        "measured_at",
        "calibration_version",
        "config_revision",
        "quality",
        "sensors",
    ],
    "properties": {
        "schema_version": {"const": "1.0"},
        **{
            key: TEXT
            for key in (
                "measurement_id",
                "measurement_cycle_id",
                "site_id",
                "edge_id",
                "calibration_version",
                "config_revision",
            )
        },
        "measured_at": {"type": "string", "format": "date-time"},
        "quality": QUALITY,
        "fill_ratio": RATIO,
        "fill_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "sensors": {"type": "array", "items": SENSOR, "minItems": 1, "maxItems": 2},
        "camera_reference": {
            "type": "object",
            "required": ["camera_id", "window_start", "window_end"],
            "properties": {
                "camera_id": TEXT,
                "window_start": {"type": "string", "format": "date-time"},
                "window_end": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": False,
        },
    },
    "allOf": [
        {
            "if": {"properties": {"quality": {"properties": {"state": {"const": "INVALID"}}}}},
            "then": {
                "not": {"anyOf": [{"required": ["fill_ratio"]}, {"required": ["fill_percent"]}]}
            },
            "else": {"required": ["fill_ratio", "fill_percent"]},
        }
    ],
    "additionalProperties": False,
}
HEARTBEAT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:ajin:edge:heartbeat:1.0",
    "type": "object",
    "required": [
        "schema_version",
        "site_id",
        "edge_id",
        "config_revision",
        "reported_at",
        "state",
        "reason_codes",
        "clock",
        "services",
    ],
    "properties": {
        "schema_version": {"const": "1.0"},
        "site_id": TEXT,
        "edge_id": TEXT,
        "config_revision": TEXT,
        "deployment_revision": TEXT,
        "camera_id": TEXT,
        "reported_at": {"type": "string", "format": "date-time"},
        "state": {
            "enum": [
                "HEALTHY",
                "DEGRADED",
                "MEASUREMENT_UNAVAILABLE",
                "OFFLINE_BUFFERING",
                "CONFIG_ERROR",
            ]
        },
        "reason_codes": REASONS,
        "clock": {
            "type": "object",
            "required": ["state", "offset_ms"],
            "properties": {
                "state": {"enum": ["SYNCED", "DEGRADED", "UNSYNCED"]},
                "offset_ms": {"type": ["number", "null"]},
            },
        },
        "services": {
            "type": "array",
            "items": {"type": "object", "required": ["service", "state", "reason_codes"]},
            "maxItems": 16,
        },
    },
    "additionalProperties": False,
}
_MEASUREMENT = Draft202012Validator(MEASUREMENT_SCHEMA, format_checker=FormatChecker())


def canonical_json(value):
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def validate_measurement(value):
    try:
        raw = canonical_json(value)
        if len(raw) > 65536:
            raise ValueError("measurement exceeds 64 KiB")
        _MEASUREMENT.validate(value)
        stamp = datetime.fromisoformat(value["measured_at"].replace("Z", "+00:00"))
        if stamp.utcoffset() != UTC.utcoffset(stamp):
            raise ValueError("measurement timestamp must be UTC")
        ids = [sensor["sensor_id"] for sensor in value["sensors"]]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate sensor")
        if value["quality"]["state"] != "INVALID" and not math.isclose(
            value["fill_percent"], value["fill_ratio"] * 100, abs_tol=1e-6
        ):
            raise ValueError("fill ratio/percent disagree")
        ref = value.get("camera_reference")
        if ref and datetime.fromisoformat(ref["window_start"]) > datetime.fromisoformat(
            ref["window_end"]
        ):
            raise ValueError("camera window reversed")
        return raw
    except (ValidationError, TypeError, KeyError, OverflowError) as error:
        # No payload or credentials in the externally returned validation error.
        raise ValueError("invalid measurement envelope") from error
