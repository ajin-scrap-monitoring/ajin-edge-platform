"""Loss-visible raw observations and deterministic periodic selection.

Coordinates are reflection points, not scrap heights or fill estimates.
"""

import hashlib
import json
import math
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import numpy as np

SAFE_INT = 9007199254740991


def utc_now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def envelope(config, message_type, *, generated_at=None):
    return {
        "schema_version": "2.0",
        "message_type": message_type,
        "message_id": str(uuid4()),
        "site_id": config["site_id"],
        "edge_id": config["edge_id"],
        "config_revision": config["config_revision"],
        "generated_at": generated_at or utc_now(),
    }


def integer(value, lower=0, upper=SAFE_INT):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("integer outside contract range")


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("invalid identifier")


def validate_configuration(config):
    for key in ("site_id", "edge_id", "config_revision"):
        identifier(config[key])
    identifier(config["coordinate_frame"]["frame_id"])
    sensors = config["sensors"]
    if len(sensors) != 2 or len({s["sensor_id"] for s in sensors}) != 2:
        raise ValueError("exactly two distinct sensors required")
    for sensor in sensors:
        identifier(sensor["sensor_id"])
        previous_end = 0
        for interval in sorted(
            sensor["angle_intervals_mdeg"], key=lambda item: item["start_inclusive"]
        ):
            start, end = interval["start_inclusive"], interval["end_exclusive"]
            integer(start, 0, 359999)
            integer(end, 1, 360000)
            if start >= end or start < previous_end:
                raise ValueError("overlapping or reversed angle intervals")
            previous_end = end
        cal = sensor["calibration"]
        if cal is None:
            continue
        identifier(cal["version"])
        if cal["verification_state"] not in ("VERIFIED", "UNVERIFIED"):
            raise ValueError("invalid calibration state")
        if type(cal["angle_sign"]) is not int or cal["angle_sign"] not in (-1, 1):
            raise ValueError("invalid angle direction")
        integer(cal["angle_zero_offset_mdeg"], -360000, 360000)
        rotation = np.asarray(cal["rotation"], dtype=float)
        translation = np.asarray(cal["translation_mm"], dtype=float)
        if (
            rotation.shape != (3, 3)
            or translation.shape != (3,)
            or not np.isfinite(rotation).all()
            or not np.isfinite(translation).all()
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8, rtol=0)
            or not math.isclose(np.linalg.det(rotation), 1, abs_tol=1e-8)
        ):
            raise ValueError("finite right-handed orthonormal calibration required")


def validate_scan(raw):
    for key in ("sensor_id", "scan_id", "stream_instance_id", "clock_domain_id"):
        identifier(raw[key])
    integer(raw["sequence"])
    mono = raw["acquired_monotonic_ns"]
    if not isinstance(mono, str) or not mono.isascii() or not mono.isdecimal():
        raise ValueError("monotonic ns must be a decimal string")
    if len(mono) > 20 or int(mono) > 2**64 - 1:
        raise ValueError("monotonic timestamp outside uint64")
    if not raw["acquired_at"].endswith(("Z", "+00:00")):
        raise ValueError("UTC timestamp required")
    datetime.fromisoformat(raw["acquired_at"].replace("Z", "+00:00"))
    if raw["timestamp_source"] != "EDGE_SCAN_RECEIVED":
        raise ValueError("unsupported timestamp source")
    if raw["clock_state"] not in ("SYNCED", "DEGRADED", "UNSYNCED"):
        raise ValueError("invalid clock state")
    offset = raw["clock_offset_ms"]
    if offset is not None and (type(offset) not in (int, float) or not math.isfinite(offset)):
        raise ValueError("invalid clock offset")
    if not isinstance(raw["samples"], list) or len(raw["samples"]) > 32768:
        raise ValueError("SDK frame capacity exceeded")
    for sample in raw["samples"]:
        integer(sample["angle_mdeg"], 0, 359999)
        integer(sample["distance_mm"], 0, (2**32 - 1) // 4)
        integer(sample["quality"], 0, 255)
        if type(sample.get("sdk_invalid_range", False)) is not bool:
            raise ValueError("invalid SDK range flag")


def sensor_header(sensor):
    cal = sensor["calibration"]
    return {
        "sensor_id": sensor["sensor_id"],
        "availability": "NO_NEW_SCAN",
        "calibration_version": cal["version"] if cal else None,
        "transform_state": cal["verification_state"] if cal else "UNAVAILABLE",
        "transform_reason_codes": (
            []
            if cal and cal["verification_state"] == "VERIFIED"
            else ["CALIBRATION_UNVERIFIED" if cal else "CALIBRATION_MISSING"]
        ),
        "angle_intervals_mdeg": deepcopy(sensor["angle_intervals_mdeg"]),
        "scan": None,
    }


def round_mm(value):
    # Explicit, symmetric half-away-from-zero wire rule; floats retained until here.
    result = int(math.copysign(math.floor(abs(value) + 0.5), value))
    integer(result, -SAFE_INT, SAFE_INT)
    return result


def transform_scan(raw, sensor):
    validate_scan(raw)
    if raw["sensor_id"] != sensor["sensor_id"]:
        raise ValueError("sensor mismatch")
    result = sensor_header(sensor)
    if not sensor["angle_intervals_mdeg"]:
        result["availability"] = "CONFIG_UNAVAILABLE"
        return result
    result["availability"] = "PRESENT"
    transformed = {
        key: deepcopy(value) for key, value in raw.items() if key not in ("sensor_id", "samples")
    }
    points = []
    cal = sensor["calibration"]
    for index, sample in enumerate(raw["samples"]):
        if not any(
            i["start_inclusive"] <= sample["angle_mdeg"] < i["end_exclusive"]
            for i in sensor["angle_intervals_mdeg"]
        ):
            continue
        point = {
            "index": index,
            **{key: sample[key] for key in ("angle_mdeg", "distance_mm", "quality")},
            "position_mm": None,
            "calculation_reason_codes": [],
        }
        # Flag is derived before q2 -> mm conversion; rounded zero is not a filter.
        if sample.get("sdk_invalid_range", False):
            point["calculation_reason_codes"] = ["SDK_INVALID_RANGE"]
        elif cal and cal["verification_state"] == "VERIFIED":
            theta = math.radians(
                (cal["angle_sign"] * sample["angle_mdeg"] + cal["angle_zero_offset_mdeg"]) / 1000
            )
            r = sample["distance_mm"]
            local = np.array([r * math.cos(theta), r * math.sin(theta), 0.0])
            position = np.asarray(cal["rotation"]) @ local + cal["translation_mm"]
            point["position_mm"] = dict(zip(("x", "y", "z"), map(round_mm, position), strict=True))
        points.append(point)
    transformed.update(
        source_point_count=len(raw["samples"]), selected_point_count=len(points), points=points
    )
    result["scan"] = transformed
    return result


class ProcessingEngine:
    def __init__(self, config, *, clock_domain_id, capacity=32):
        validate_configuration(config)
        identifier(clock_domain_id)
        if capacity < 1:
            raise ValueError("positive capacity required")
        self.config = deepcopy(config)
        self.domain = clock_domain_id
        self.capacity = capacity
        self.buffers = {s["sensor_id"]: deque() for s in config["sensors"]}
        self.sensors = {s["sensor_id"]: s for s in self.config["sensors"]}
        self.seen = OrderedDict()
        self.highwater = OrderedDict()
        self.rejected = set()
        self.dropped_scans = 0

    def ingest(self, raw):
        sensor_id = raw.get("sensor_id")
        try:
            validate_scan(raw)
            if sensor_id not in self.sensors or raw["clock_domain_id"] != self.domain:
                raise ValueError("unconfigured sensor or clock domain")
            digest = hashlib.sha256(canonical(raw)).digest()
            scan_id = raw["scan_id"]
            if scan_id in self.seen:
                if self.seen[scan_id] != digest:
                    raise ValueError("conflicting scan identity")
                return None
            stream = (sensor_id, raw["stream_instance_id"])
            if raw["sequence"] <= self.highwater.get(stream, -1):
                raise ValueError("replayed or reordered scan sequence")
            observation = transform_scan(raw, self.sensors[sensor_id])
            self.highwater[stream] = raw["sequence"]
            self.highwater.move_to_end(stream)
            while len(self.highwater) > 64:
                self.highwater.popitem(last=False)
            self.seen[scan_id] = digest
            while len(self.seen) > self.capacity * 4:
                self.seen.popitem(last=False)
            queue = self.buffers[sensor_id]
            if len(queue) == self.capacity:
                queue.popleft()
                self.dropped_scans += 1
            queue.append(observation)
            self.rejected.discard(sensor_id)
            return deepcopy(observation)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            if sensor_id in self.sensors:
                self.rejected.add(sensor_id)
            raise ValueError("invalid scan") from error

    def periodic(self, *, now_ns, generated_at=None):
        candidates = {}
        for sensor_id, queue in self.buffers.items():
            candidates[sensor_id] = [
                item
                for item in queue
                if item["scan"] is not None
                and 0 <= now_ns - int(item["scan"]["acquired_monotonic_ns"]) <= 1000000000
            ]
        a, b = self.sensors
        pairs = [
            (left, right)
            for left in candidates[a]
            for right in candidates[b]
            if abs(self._time(left) - self._time(right)) <= 100000000
        ]
        if pairs:
            chosen = max(
                pairs,
                key=lambda p: (min(map(self._time, p)), -abs(self._time(p[0]) - self._time(p[1]))),
            )
        else:
            chosen = tuple(max(candidates[s], key=self._time, default=None) for s in (a, b))
        present = sum(item is not None for item in chosen)
        state = (
            ("MATCHED" if pairs else "UNMATCHED")
            if present == 2
            else ("PARTIAL" if present else "NONE")
        )
        delta = (
            abs(self._time(chosen[0]) - self._time(chosen[1])) / 1000000 if present == 2 else None
        )
        sensors = []
        for sensor_id, selected in zip((a, b), chosen, strict=True):
            queue = self.buffers[sensor_id]
            if selected is not None:
                sensors.append(deepcopy(selected))
                # Consume selected and older candidates: do not report backwards in time.
                self.buffers[sensor_id] = deque(
                    item
                    for item in queue
                    if item["scan"] and self._time(item) > self._time(selected)
                )
            else:
                header = sensor_header(self.sensors[sensor_id])
                header["availability"] = (
                    "CONFIG_UNAVAILABLE"
                    if not header["angle_intervals_mdeg"]
                    else "REJECTED"
                    if sensor_id in self.rejected
                    else "STALE"
                    if queue
                    else "NO_NEW_SCAN"
                )
                sensors.append(header)
                queue.clear()
        return {
            **envelope(self.config, "lidar_measurement", generated_at=generated_at),
            "delivery_kind": "PERIODIC",
            "report_interval_ms": 1000,
            "selection_policy": "LATEST_TIME_MATCHED_PAIR",
            "coordinate_frame_id": self.config["coordinate_frame"]["frame_id"],
            "pair_state": state,
            "pair_delta_ms": delta,
            "sensors": sensors,
        }

    @staticmethod
    def _time(item):
        return int(item["scan"]["acquired_monotonic_ns"])
