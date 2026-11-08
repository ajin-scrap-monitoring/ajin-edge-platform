"""Independent sensor sections and explicit calibrated feature fusion. Raw input is immutable."""

import json
import math
from collections import deque
from copy import deepcopy
from threading import Lock

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .judgment_schema import JUDGMENT_SCHEMA
from .processing import copy_observation
from .section_profile import build_profile, geometry_matches


def is_independent(model):
    return bool(model and model.get("model_type") == "INDEPENDENT_SECTIONS_V1")


def fill_enabled(model):
    return bool(
        is_independent(model)
        and model["verification_state"] == "VERIFIED"
        and model["fusion"]
        and model["fusion"]["verification_state"] == "VERIFIED"
    )


def validate_curve(curve):
    if (
        curve[0][0] != 0
        or curve[-1][0] != 1
        or any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(curve, curve[1:], strict=False))
    ):
        raise ValueError("calibration must be monotone with input endpoints 0 and 1")


def validate_judgment(model, sensor_ids=None):
    json.dumps(model, allow_nan=False)
    try:
        Draft202012Validator(JUDGMENT_SCHEMA).validate(model)
    except ValidationError as error:
        raise ValueError("invalid judgment settings") from error
    window = model["window"]
    if (
        any(type(v) is not int for v in window.values())
        or window["min_scans"] > window["scan_count"]
    ):
        raise ValueError("invalid integer judgment window")
    if is_independent(model):
        sections = model["sections"]
        ids = {s["sensor_id"] for s in sections}
        if len(ids) != 2 or (sensor_ids is not None and ids != set(sensor_ids)):
            raise ValueError("sections must name the two registered sensors exactly")
    else:
        # Legacy schema is accepted for stored/raw registration compatibility, not calculation.
        sections = [
            {
                **model["section"],
                "filters": model["filters"],
                "max_interpolation_gap_bins": model["max_interpolation_gap_bins"],
            }
        ]
        if model["fill_calibration"] is not None:
            validate_curve(model["fill_calibration"])
    for section in sections:
        filters = section["filters"]
        integers = [
            section["bin_width_mm"],
            section["max_interpolation_gap_bins"],
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
        n = len(section["floor_z_mm"])
        if (
            any(type(v) is not int for v in integers)
            or len(section["ceiling_z_mm"]) != n
            or len(section["active_bins"]) != n
            or not any(section["active_bins"])
            or any(
                b <= a for a, b in zip(section["floor_z_mm"], section["ceiling_z_mm"], strict=True)
            )
            or not math.isclose(sum(x * x for x in section["axis_xy"]), 1, abs_tol=1e-8)
            or filters["distance_min_mm"] > filters["distance_max_mm"]
        ):
            raise ValueError("inconsistent section geometry, filter or integer parameter")
    if is_independent(model) and model["fusion"] is not None:
        components = model["fusion"]["components"]
        if (
            {c["sensor_id"] for c in components} != ids
            or any(c["weight"] <= 0 for c in components)
            or not math.isclose(
                math.fsum(c["weight"] for c in components), 1, abs_tol=1e-9, rel_tol=0
            )
        ):
            raise ValueError(
                "fusion requires explicit positive weights for both sensors summing to one"
            )
        for component in components:
            validate_curve(component["calibration"])


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
        count = self.model["window"]["scan_count"] if is_independent(self.model) else 1
        self.history = {s["sensor_id"]: deque(maxlen=count) for s in self.config["sensors"]}

    def ingest(self, observation):
        if not is_independent(self.model):
            return
        scan = observation["scan"]
        owned = copy_observation(observation)
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
            history.append(owned)

    def calculate(self, now_ns):
        if not self.model:
            return None, ["FILL_METHOD_NOT_CONFIGURED"], []
        if not is_independent(self.model):
            return None, ["LEGACY_SECTION_MODEL_UNSUPPORTED"], []
        if self.model["verification_state"] != "VERIFIED":
            return None, ["FILL_METHOD_NOT_VALIDATED"], []
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
        anchors = self._anchors(histories)
        sensors = {s["sensor_id"]: s for s in self.config["sensors"]}
        results, used = [], []
        for section in self.model["sections"]:
            sid = section["sensor_id"]
            history = histories[sid]
            result, inputs = self._section(section, sensors[sid], history)
            results.append(result)
            used.extend(inputs)
        reasons = {r for result in results for r in result["reason_codes"]}
        if all(histories.values()) and anchors is None:
            reasons.add("SENSOR_TIME_MISMATCH")
        fusion = self.model["fusion"]
        combined = None
        if fusion is None:
            reasons.add("FILL_FUSION_NOT_CONFIGURED")
        elif fusion["verification_state"] != "VERIFIED":
            reasons.add("FILL_FUSION_NOT_VALIDATED")
        elif anchors is not None and all(r["state"] != "UNAVAILABLE" for r in results):
            ratios = {r["sensor_id"]: r["profile"]["area_ratio"] for r in results}
            components = [
                {
                    "sensor_id": c["sensor_id"],
                    "weight": c["weight"],
                    "section_ratio": ratios[c["sensor_id"]],
                    "anchor_scan_id": next(
                        o["scan"]["scan_id"]
                        for o in histories[c["sensor_id"]]
                        if int(o["scan"]["acquired_monotonic_ns"]) == anchors[c["sensor_id"]]
                    ),
                    "calibrated_ratio": mapped_fill(ratios[c["sensor_id"]], c["calibration"]),
                }
                for c in fusion["components"]
            ]
            ratio = math.fsum(c["weight"] * c["calibrated_ratio"] for c in components) / math.fsum(
                c["weight"] for c in components
            )
            combined = {
                "method": fusion["method"],
                "model_version": fusion["version"],
                "ratio": ratio,
                "components": components,
                "pair_delta_ms": (max(anchors.values()) - min(anchors.values())) / 1000000,
                "clock_domain_id": next(iter(histories.values()))[-1]["scan"]["clock_domain_id"],
            }
        return {"section_profiles": results, "fusion_result": combined}, sorted(reasons), used

    def _anchors(self, histories):
        if any(not h for h in histories.values()):
            return None
        # Fuse the latest anchors of independently computed profiles. An older
        # matching pair must not erase a current profile or hide current skew.
        pair = [history[-1] for history in histories.values()]
        stamps = [int(o["scan"]["acquired_monotonic_ns"]) for o in pair]
        if (
            len({o["scan"]["clock_domain_id"] for o in pair}) != 1
            or max(stamps) - min(stamps) > self.model["window"]["pair_tolerance_ms"] * 1000000
        ):
            return None
        return {
            sid: int(o["scan"]["acquired_monotonic_ns"])
            for sid, o in zip(histories, pair, strict=True)
        }

    def _section(self, section, sensor, history):
        result = {
            "sensor_id": section["sensor_id"],
            "state": "UNAVAILABLE",
            "reason_codes": [],
            "profile": None,
        }
        cal = sensor["calibration"]
        reason = None
        if cal is None:
            reason = "CALIBRATION_MISSING"
        elif cal["verification_state"] != "VERIFIED":
            reason = "CALIBRATION_UNVERIFIED"
        elif not geometry_matches(section, cal):
            reason = "SECTION_GEOMETRY_MISMATCH"
        elif not history:
            reason = "INSUFFICIENT_INPUT"
        elif len(history) < self.model["window"]["min_scans"]:
            reason = "INSUFFICIENT_HISTORY"
        elif any(
            o["transform_state"] != "VERIFIED" or o["calibration_version"] != cal["version"]
            for o in history
        ):
            reason = "CALIBRATION_MISMATCH"
        if reason:
            result["reason_codes"] = [reason]
            return result, []
        profile = build_profile(self.config, section, cal, history)
        result["profile"] = profile
        if profile["area_ratio"] is None:
            result["reason_codes"] = ["SECTION_INCOMPLETE"]
        elif any(b["source"] == "INTERPOLATED" for b in profile["bins"]):
            result.update(state="LIMITED", reason_codes=["INTERPOLATED_BINS"])
        else:
            result["state"] = "AVAILABLE"
        return result, history
