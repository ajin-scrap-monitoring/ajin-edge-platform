"""Machine-readable wire types plus cross-field invariants."""

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from .processing import SAFE_INT, canonical, identifier, integer, validate_configuration


def obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def array(items, **limits):
    return {"type": "array", "items": items, **limits}


def enum(*values):
    return {"enum": list(values)}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


ID = {"type": "string", "minLength": 1, "maxLength": 128}
UINT = {"type": "integer", "minimum": 0, "maximum": SAFE_INT}
INT = {"type": "integer", "minimum": -SAFE_INT, "maximum": SAFE_INT}
NUMBER = {"type": "number"}
TIME = {"type": "string", "format": "date-time", "pattern": r"(Z|\+00:00)$"}
REASONS = array(ID, uniqueItems=True)
INTERVAL = obj(
    {
        "start_inclusive": {"type": "integer", "minimum": 0, "maximum": 359999},
        "end_exclusive": {"type": "integer", "minimum": 1, "maximum": 360000},
    }
)
POINT = obj(
    {
        "index": UINT,
        "angle_mdeg": {"type": "integer", "minimum": 0, "maximum": 359999},
        "distance_mm": {"type": "integer", "minimum": 0, "maximum": (2**32 - 1) // 4},
        "quality": {"type": "integer", "minimum": 0, "maximum": 255},
        "position_mm": nullable(obj({"x": INT, "y": INT, "z": INT})),
        "calculation_reason_codes": REASONS,
    }
)
SCAN = obj(
    {
        "scan_id": ID,
        "stream_instance_id": ID,
        "sequence": UINT,
        "acquired_at": TIME,
        "timestamp_source": enum("EDGE_SCAN_RECEIVED"),
        "clock_domain_id": ID,
        "acquired_monotonic_ns": {"type": "string", "pattern": "^[0-9]{1,20}$"},
        "clock_state": enum("SYNCED", "DEGRADED", "UNSYNCED"),
        "clock_offset_ms": nullable(NUMBER),
        "source_point_count": UINT,
        "selected_point_count": UINT,
        "points": array(POINT),
    }
)
SENSOR = obj(
    {
        "sensor_id": ID,
        "availability": enum("PRESENT", "NO_NEW_SCAN", "STALE", "REJECTED", "CONFIG_UNAVAILABLE"),
        "calibration_version": nullable(ID),
        "transform_state": enum("VERIFIED", "UNVERIFIED", "UNAVAILABLE"),
        "transform_reason_codes": REASONS,
        "angle_intervals_mdeg": array(INTERVAL),
        "scan": nullable(SCAN),
    }
)
REF = obj(
    {
        "scan_id": ID,
        "sensor_id": ID,
        "config_revision": ID,
        "calibration_version": nullable(ID),
        "coordinate_frame_id": ID,
    }
)
VISION_REF = obj(
    {
        "camera_id": ID,
        "stream_instance_id": ID,
        "frame_sequence": UINT,
        "frame_acquired_at": TIME,
        "timestamp_source": ID,
        "observation_id": ID,
        "model_version": ID,
    }
)


def message_schema(kind, properties, optional=None):
    common = {
        "schema_version": enum("2.0"),
        "message_type": enum(kind),
        "message_id": ID,
        "site_id": ID,
        "edge_id": ID,
        "generated_at": TIME,
        "config_revision": ID,
    }
    schema = obj({**common, **properties})
    schema["properties"].update(optional or {})
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


MEASUREMENT_SCHEMA = message_schema(
    "lidar_measurement",
    {
        "delivery_kind": enum("PERIODIC", "EVIDENCE"),
        "report_interval_ms": nullable(enum(1000)),
        "selection_policy": enum("LATEST_TIME_MATCHED_PAIR", "REFERENCED_SCANS"),
        "coordinate_frame_id": ID,
        "pair_state": enum("MATCHED", "UNMATCHED", "PARTIAL", "NONE", "NOT_APPLICABLE"),
        "pair_delta_ms": nullable({"type": "number", "minimum": 0}),
        "sensors": array(SENSOR, minItems=1, maxItems=2),
    },
    {"assessment_ids": array(ID, minItems=1, uniqueItems=True), "original_message_id": ID},
)
ASSESSMENT_SCHEMA = message_schema(
    "edge_assessment",
    {
        "assessment_type": enum("STATE", "EVENT"),
        "assessment_id": ID,
        "revision": {"type": "integer", "minimum": 1, "maximum": SAFE_INT},
        "observed_from": nullable(TIME),
        "observed_to": nullable(TIME),
        "rule_version": nullable(ID),
        "decision_state": enum("AVAILABLE", "LIMITED", "UNAVAILABLE"),
        "reason_codes": REASONS,
        "fill_state": enum("AVAILABLE", "UNAVAILABLE"),
        "fill_estimate": nullable(
            obj({"ratio": {"type": "number", "minimum": 0, "maximum": 1}, "method_version": ID})
        ),
        "event": nullable(
            obj(
                {
                    "kind": enum(
                        "FILL_INCREASE",
                        "FILL_DECREASE",
                        "OCCLUSION",
                        "COLLECTION",
                        "GRAPPLE_PRESENT",
                    ),
                    "stage": enum("SUSPECTED", "CONFIRMED", "RESOLVED", "RETRACTED"),
                }
            )
        ),
        "capabilities": obj(
            {
                key: enum("NOT_IMPLEMENTED", "NOT_VALIDATED", "ENABLED")
                for key in ("fill", "occlusion", "collection", "vision")
            }
        ),
        "evidence": obj({"scans": array(REF), "vision_observations": array(VISION_REF)}),
    },
    {"original_message_id": ID},
)
STATUS_SCHEMA = message_schema(
    "edge_status",
    {
        "orchestrator_instance_id": ID,
        "sequence": UINT,
        "overall_state": enum(
            "HEALTHY", "DEGRADED", "MEASUREMENT_UNAVAILABLE", "CONFIG_ERROR", "UNKNOWN"
        ),
        "reason_codes": REASONS,
        "clock": obj(
            {"state": enum("SYNCED", "DEGRADED", "UNSYNCED"), "offset_ms": nullable(NUMBER)}
        ),
        "sensors": array(
            obj(
                {
                    "sensor_id": ID,
                    "data_state": enum("FRESH", "STALE", "RECOVERING", "UNAVAILABLE", "UNKNOWN"),
                    "last_scan_at": nullable(TIME),
                    "reason_codes": REASONS,
                }
            )
        ),
        "services": array(
            obj(
                {
                    "service": ID,
                    "state": enum(
                        "STARTING", "HEALTHY", "DEGRADED", "RETRYING", "FATAL", "UNKNOWN"
                    ),
                    "last_progress_at": nullable(TIME),
                    "reason_codes": REASONS,
                }
            )
        ),
        "delivery": obj(
            {
                "pending_messages": UINT,
                "pending_bytes": UINT,
                "quarantined_messages": UINT,
                "last_backend_ack_at": nullable(TIME),
            }
        ),
        "storage": obj(
            {
                "limit_bytes": UINT,
                "used_bytes": UINT,
                "lost_messages": UINT,
                "loss_from": nullable(TIME),
                "loss_to": nullable(TIME),
                "last_loss_reason": nullable(ID),
                "unavailable_evidence_scan_ids": array(ID, maxItems=128),
            }
        ),
    },
)
ROW = array(NUMBER, minItems=3, maxItems=3)
CONFIGURATION_SCHEMA = message_schema(
    "edge_configuration",
    {
        "coordinate_frame": obj(
            {
                "frame_id": ID,
                "unit": enum("mm"),
                "origin_description": ID,
                "x_axis_description": ID,
                "y_axis_description": ID,
                "z_axis_description": ID,
                "handedness": enum("RIGHT_HANDED"),
            }
        ),
        "sensors": array(
            obj(
                {
                    "sensor_id": ID,
                    "angle_intervals_mdeg": array(INTERVAL),
                    "calibration": nullable(
                        obj(
                            {
                                "version": ID,
                                "verification_state": enum("VERIFIED", "UNVERIFIED"),
                                "angle_sign": enum(-1, 1),
                                "angle_zero_offset_mdeg": INT,
                                "rotation": array(ROW, minItems=3, maxItems=3),
                                "translation_mm": ROW,
                            }
                        )
                    ),
                }
            ),
            minItems=2,
            maxItems=2,
        ),
    },
)
SCHEMAS = {
    "lidar_measurement": MEASUREMENT_SCHEMA,
    "edge_assessment": ASSESSMENT_SCHEMA,
    "edge_status": STATUS_SCHEMA,
    "edge_configuration": CONFIGURATION_SCHEMA,
}
VALIDATORS = {
    key: Draft202012Validator(schema, format_checker=FormatChecker())
    for key, schema in SCHEMAS.items()
}


def validate_message(payload):
    try:
        canonical(payload)  # Reject non-finite floats, including deep extensions.
        kind = payload["message_type"]
        structural = payload
        if kind == "lidar_measurement":
            # The object schema is unchanged publicly. Validate the repetitive point array
            # directly below rather than recursively interpreting the same schema per point.
            structural = {
                **payload,
                "sensors": [
                    {
                        **sensor,
                        "scan": {**sensor["scan"], "points": []}
                        if sensor["scan"] is not None
                        else None,
                    }
                    for sensor in payload["sensors"]
                ],
            }
        VALIDATORS[kind].validate(structural)
        if kind == "edge_configuration":
            validate_configuration(payload)
        elif kind == "lidar_measurement":
            _measurement(payload)
        elif kind == "edge_assessment":
            _assessment(payload)
    except (ValidationError, KeyError, TypeError, OverflowError) as error:
        raise ValueError("invalid v2 message") from error


def _measurement(payload):
    sensors = payload["sensors"]
    if len({s["sensor_id"] for s in sensors}) != len(sensors):
        raise ValueError("duplicate sensor")
    present = []
    for sensor in sensors:
        scan = sensor["scan"]
        if (sensor["availability"] == "PRESENT") != (scan is not None):
            raise ValueError("availability/scan mismatch")
        if scan is None:
            continue
        present.append(scan)
        points = scan["points"]
        if not isinstance(points, list):
            raise ValueError("points must be an array")
        for point in points:
            _point(point)
        if len(points) != scan["selected_point_count"] or len(points) > scan["source_point_count"]:
            raise ValueError("point count mismatch")
        indices = [p["index"] for p in points]
        if indices != sorted(set(indices)) or any(i >= scan["source_point_count"] for i in indices):
            raise ValueError("invalid source indices")
        for point in points:
            if not any(
                i["start_inclusive"] <= point["angle_mdeg"] < i["end_exclusive"]
                for i in sensor["angle_intervals_mdeg"]
            ):
                raise ValueError("point outside selected angles")
            if sensor["transform_state"] != "VERIFIED" and point["position_mm"] is not None:
                raise ValueError("unverified coordinates")
    if payload["delivery_kind"] == "EVIDENCE":
        if (
            len(present) != len(sensors)
            or not payload.get("assessment_ids")
            or payload["pair_state"] != "NOT_APPLICABLE"
            or payload["pair_delta_ms"] is not None
            or payload["report_interval_ms"] is not None
            or payload["selection_policy"] != "REFERENCED_SCANS"
        ):
            raise ValueError("invalid evidence envelope")
        return
    if (
        len(sensors) != 2
        or "assessment_ids" in payload
        or payload["report_interval_ms"] != 1000
        or payload["selection_policy"] != "LATEST_TIME_MATCHED_PAIR"
    ):
        raise ValueError("invalid periodic envelope")
    if len(present) < 2:
        if (
            payload["pair_state"] != ("PARTIAL" if present else "NONE")
            or payload["pair_delta_ms"] is not None
        ):
            raise ValueError("invalid partial pair")
    else:
        same_clock = present[0]["clock_domain_id"] == present[1]["clock_domain_id"]
        delta = (
            abs(int(present[0]["acquired_monotonic_ns"]) - int(present[1]["acquired_monotonic_ns"]))
            / 1000000
            if same_clock
            else None
        )
        expected = "MATCHED" if delta is not None and delta <= 100 else "UNMATCHED"
        if payload["pair_state"] != expected or payload["pair_delta_ms"] != delta:
            raise ValueError("pair metadata does not match scans")


def _point(point):
    if not isinstance(point, dict) or point.keys() != POINT["properties"].keys():
        raise ValueError("invalid point fields")
    integer(point["index"])
    integer(point["angle_mdeg"], 0, 359999)
    integer(point["distance_mm"], 0, (2**32 - 1) // 4)
    integer(point["quality"], 0, 255)
    reasons = point["calculation_reason_codes"]
    if not isinstance(reasons, list):
        raise ValueError("invalid point reasons")
    for reason in reasons:
        identifier(reason)
    if len(set(reasons)) != len(reasons):
        raise ValueError("duplicate point reason")
    position = point["position_mm"]
    if position is not None:
        if not isinstance(position, dict) or set(position) != {"x", "y", "z"}:
            raise ValueError("invalid position fields")
        for value in position.values():
            integer(value, -SAFE_INT, SAFE_INT)


def _assessment(payload):
    if (payload["assessment_type"] == "STATE") != (payload["event"] is None):
        raise ValueError("event/type mismatch")
    if payload["assessment_type"] == "STATE" and payload["revision"] != 1:
        raise ValueError("state snapshot revision must be one")
    if (payload["fill_state"] == "UNAVAILABLE") != (payload["fill_estimate"] is None):
        raise ValueError("fill availability mismatch")
    start, end = payload["observed_from"], payload["observed_to"]
    if (start is None) != (end is None):
        raise ValueError("incomplete observation interval")
    if start is None and payload["evidence"]["scans"]:
        raise ValueError("scan references require observation interval")
    if start is not None:
        from datetime import datetime

        if datetime.fromisoformat(start) > datetime.fromisoformat(end):
            raise ValueError("reversed observation interval")
