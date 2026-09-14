"""Deterministic processing; times are supplied explicitly for replay."""

import math
import uuid
from collections import deque
from datetime import UTC, datetime

import numpy as np


def utc(ms):
    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def inside(px, pz, polygon):
    hit = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (a[1] > pz) != (b[1] > pz) and px < (b[0] - a[0]) * (pz - a[1]) / (b[1] - a[1]) + a[0]:
            hit = not hit
    return hit


def mapping(value):
    a = np.asarray(value, dtype=float)
    if (
        a.ndim != 2
        or a.shape[1] != 2
        or len(a) < 2
        or not np.isfinite(a).all()
        or a[0, 0] != 0
        or a[-1, 0] != 1
        or np.any(np.diff(a[:, 0]) <= 0)
        or np.any(np.diff(a[:, 1]) < 0)
        or np.any((a < 0) | (a > 1))
    ):
        raise ValueError("invalid monotone calibration map")
    return a


class ProcessingEngine:
    def __init__(self, config):
        self.config = config
        self.sensors = {s["sensor_id"]: s for s in config["sensors"]}
        if not self.sensors or len(self.sensors) != len(config["sensors"]):
            raise ValueError("unique sensors required")
        weights = [s["base_weight"] for s in self.sensors.values()]
        if any(not math.isfinite(w) or w <= 0 for w in weights) or not math.isclose(
            sum(weights), 1, abs_tol=1e-6
        ):
            raise ValueError("weights must sum to one")
        cal = config["calibration"]
        if not cal["version"] or (cal.get("demo") and not config.get("allow_demo_calibration")):
            raise ValueError("demo calibration requires explicit opt-in")
        self.fusion_map = mapping(cal["fusion_map"])
        self.single_maps = {k: mapping(v) for k, v in cal.get("single_sensor_maps", {}).items()}
        self.hints = cal.get("candidate_hints")
        self.hint_baseline = None
        self.hint_version = cal["version"]
        if self.hints is not None:
            try:
                h = self.hints
                if not 0 < h["window_seconds"] <= 2:
                    raise ValueError("candidate window")
                for key in ("rapid_rise_ratio", "collection_drop_ratio", "coverage_drop_ratio"):
                    if not 0 < h[key] <= 1:
                        raise ValueError("candidate ratio")
                if not 0 < h["occlusion_change_mm"] < 100000:
                    raise ValueError("candidate height")
                for key in ("occlusion_width_mm", "occlusion_shift_mm"):
                    if len(h[key]) != 2 or not 0 < h[key][0] <= h[key][1] < 100000:
                        raise ValueError("candidate geometry")
            except (KeyError, TypeError):
                raise ValueError("complete explicit candidate hint calibration required") from None
        self.target_hz = config.get("processing", {}).get("target_scan_hz", 10)
        if not math.isfinite(self.target_hz) or self.target_hz <= 0:
            raise ValueError("target rate")
        self.geometry = {}
        self.filters = {}
        for sid, s in self.sensors.items():
            filters = s.get("sample_filter", {})
            minimum = filters.get("distance_min_mm", 1)
            maximum_distance = filters.get("distance_max_mm", 100000)
            quality = filters.get("quality_min", 10)
            angles = filters.get("angle_interval_mdeg", [0, 360000])
            if (
                not all(math.isfinite(v) for v in (minimum, maximum_distance, quality))
                or not 0 < minimum < maximum_distance <= 100000
                or not 0 <= quality <= 63
                or len(angles) != 2
                or not all(math.isfinite(v) for v in angles)
                or not 0 <= angles[0] < angles[1] <= 360000
            ):
                raise ValueError("invalid sample filter")
            self.filters[sid] = (minimum, maximum_distance, quality, angles)
            c = s["calibration"]
            r = np.asarray(c["rotation"], float)
            t = np.asarray(c["translation_mm"], float)
            if (
                r.shape != (3, 3)
                or t.shape != (3,)
                or not np.isfinite(r).all()
                or not np.isfinite(t).all()
                or not np.allclose(r @ r.T, np.eye(3), atol=1e-6)
                or not np.isclose(np.linalg.det(r), 1)
            ):
                raise ValueError("invalid rigid transform")
            x = np.asarray(c["roi_x_mm"], float)
            z = np.asarray(c["roi_z_mm"], float)
            if (
                x.shape != (2,)
                or z.shape != (2,)
                or not np.isfinite(x).all()
                or not np.isfinite(z).all()
                or x[1] <= x[0]
                or z[1] <= z[0]
                or not np.isclose((x[1] - x[0]) % 50, 0)
            ):
                raise ValueError("ROI must have 50mm bins")
            n = round((x[1] - x[0]) / 50)
            bottom = np.asarray(c["bottom_mm"], float)
            maximum = np.asarray(c["max_height_mm"], float)
            if (
                bottom.shape != (n,)
                or maximum.shape != (n,)
                or not np.isfinite(bottom).all()
                or not np.isfinite(maximum).all()
                or np.any(maximum <= bottom)
            ):
                raise ValueError("invalid section profile")
            masks = list(c.get("masks_x_mm", []))
            for polygon in [c.get("roi_polygon_xz_mm")] + c.get("mask_polygons_xz_mm", []):
                if polygon is not None and (
                    len(polygon) < 3
                    or np.asarray(polygon).shape != (len(polygon), 2)
                    or not np.isfinite(polygon).all()
                ):
                    raise ValueError("invalid polygon")
            if any(
                len(m) != 2 or not all(math.isfinite(v) for v in m) or m[1] <= m[0] for m in masks
            ):
                raise ValueError("invalid masks")
            for polygon in c.get("mask_polygons_xz_mm", []):
                points = [tuple(point) for point in polygon]
                lo = min(point[0] for point in points)
                hi = max(point[0] for point in points)
                low_z = min(point[1] for point in points)
                high_z = max(point[1] for point in points)
                corners = {(lo, low_z), (hi, low_z), (hi, high_z), (lo, high_z)}
                if (
                    len(points) != 4
                    or set(points) != corners
                    or low_z > z[0]
                    or high_z < z[1]
                    or any(
                        a[0] != b[0] and a[1] != b[1]
                        for a, b in zip(points, points[1:] + points[:1], strict=True)
                    )
                ):
                    raise ValueError("fixed masks must be full-column rectangles")
                masks.append([lo, hi])
            if any(
                not x[0] <= lo < hi <= x[1]
                or not np.isclose((lo - x[0]) % 50, 0)
                or not np.isclose((hi - x[0]) % 50, 0)
                for lo, hi in masks
            ):
                raise ValueError("fixed masks must be bin-aligned full-column exclusions")
            centers = x[0] + 25 + np.arange(n) * 50
            active = np.ones(n, dtype=bool)
            for lo, hi in masks:
                active &= ~((centers >= lo) & (centers < hi))
            if not active.any():
                raise ValueError("empty section")
            self.geometry[sid] = (r, t, x, z, bottom, maximum, active, masks)
        self.buffers = {s: deque(maxlen=10) for s in self.sensors}
        self.validated_history = {s: set() for s in self.sensors}
        self.last = {}
        self.blocked = set()
        self.frame_loss = 0
        self.history_evictions = 0
        self.used = {}
        self.last_signature = None
        self.pair_wait_started = None

    @staticmethod
    def _time_valid(f, monotonic_ns, unix_ms):
        return (
            0 <= monotonic_ns - f.acquired_monotonic_ns <= 2_000_000_000
            and abs(
                (unix_ms - f.acquired_at_unix_ms) - (monotonic_ns - f.acquired_monotonic_ns) / 1e6
            )
            <= 500
        )

    def ingest(self, f, *, receive_monotonic_ns=None, receive_unix_ms=None):
        sid = f.sensor_id
        if sid not in self.sensors:
            self.frame_loss += 1
            return False
        if f.config_revision != self.config["config_revision"]:
            self.blocked.add(sid)
            self.buffers[sid].clear()
            self.frame_loss += 1
            return False
        if (
            f.schema_version != "1.0"
            or f.edge_id != self.config["edge_id"]
            or not f.instance_id
            or f.sdk_status != "OK"
            or not 0 < len(f.samples) <= 32768
            or not math.isfinite(f.scan_hz)
            or f.scan_hz <= 0
            or f.acquired_monotonic_ns <= 0
            or any(not 0 <= sample.angle_mdeg < 360000 for sample in f.samples)
        ):
            self.frame_loss += 1
            return False
        if (receive_monotonic_ns is None) != (receive_unix_ms is None):
            raise ValueError("both receive clocks required")
        if receive_monotonic_ns is not None and not self._time_valid(
            f, receive_monotonic_ns, receive_unix_ms
        ):
            self.frame_loss += 1
            return False
        old = self.last.get(sid)
        if (
            old
            and old.instance_id == f.instance_id
            and (f.sequence <= old.sequence or f.acquired_monotonic_ns <= old.acquired_monotonic_ns)
        ):
            self.frame_loss += 1
            return False
        if self.buffers[sid] and self.buffers[sid][-1].instance_id != f.instance_id:
            self.buffers[sid].clear()
        if any(
            p.instance_id == f.instance_id and p.sequence == f.sequence for p in self.buffers[sid]
        ):
            self.frame_loss += 1
            return False
        self.blocked.discard(sid)
        # Replay has no receive clock: commit sequence/time history only after
        # measure validates its supplied clocks. Runtime can reject immediately.
        if len(self.buffers[sid]) == 10:
            self.history_evictions += 1
        self.buffers[sid].append(f)
        return True

    def _profile(self, sid, frames):
        r, t, x, z, bottom, maximum, active, masks = self.geometry[sid]
        n = len(bottom)
        history = []
        eligible = valid = 0
        minimum, maximum_distance, quality_min, angles = self.filters[sid]
        for f in frames[-5:]:
            buckets = [[] for _ in range(n)]
            for p in f.samples:
                if not angles[0] <= p.angle_mdeg < angles[1]:
                    continue
                if not (
                    minimum <= p.distance_mm <= maximum_distance and quality_min <= p.quality <= 63
                ):
                    # An expected-angle return with unusable range/quality cannot
                    # be located geometrically; count it as invalid, not absent.
                    eligible += 1
                    continue
                theta = -p.angle_mdeg * math.pi / 180000
                point = (
                    r
                    @ np.array(
                        [p.distance_mm * math.cos(theta), p.distance_mm * math.sin(theta), 0]
                    )
                    + t
                )
                px, pz = point[0], point[2]
                if not x[0] <= px < x[1] or any(lo <= px < hi for lo, hi in masks):
                    continue
                c = self.sensors[sid]["calibration"]
                if c.get("roi_polygon_xz_mm") and not inside(px, pz, c["roi_polygon_xz_mm"]):
                    continue
                if any(inside(px, pz, p) for p in c.get("mask_polygons_xz_mm", [])):
                    continue
                eligible += 1
                if not z[0] <= pz <= z[1]:
                    continue
                valid += 1
                buckets[min(n - 1, int((px - x[0]) / 50))].append(pz)
            history.append(np.array([np.median(b) if b else np.nan for b in buckets]))
        a = np.array(history)
        occlusion = False
        if (
            self.hints
            and len(a) >= 3
            and frames[-1].acquired_monotonic_ns - frames[-3].acquired_monotonic_ns
            <= self.hints["window_seconds"] * 1e9
        ):
            centers = []
            for profile in a[-3:]:
                values = profile[active & np.isfinite(profile)]
                if not len(values):
                    break
                indices = np.flatnonzero(
                    active & (profile >= np.median(values) + self.hints["occlusion_change_mm"])
                )
                if not len(indices) or np.any(np.diff(indices) != 1):
                    break
                width = len(indices) * 50
                if (
                    not self.hints["occlusion_width_mm"][0]
                    <= width
                    <= self.hints["occlusion_width_mm"][1]
                ):
                    break
                centers.append(float(np.mean(indices)) * 50)
            if len(centers) == 3:
                shifts = np.diff(centers)
                lo, hi = self.hints["occlusion_shift_mm"]
                occlusion = bool(
                    np.all((np.abs(shifts) >= lo) & (np.abs(shifts) <= hi))
                    and shifts[0] * shifts[1] > 0
                )
        result = np.full(n, np.nan)
        stability = []
        for i in range(n):
            values = a[:, i]
            values = values[np.isfinite(values)]
            if not len(values):
                continue
            center = float(np.median(values))
            mad = float(np.median(np.abs(values - center)))
            threshold = max(10, 3 * mad)
            accepted = values[np.abs(values - center) <= threshold]
            result[i] = np.median(accepted)
            stability.append(len(accepted) / len(values))
        observed = np.isfinite(result) & active
        coverage = float(observed.sum() / active.sum())
        # Interior gaps only; never bridge a fixed mask.
        for i in range(n):
            if not active[i] or np.isfinite(result[i]):
                continue
            left = i - 1
            while left >= 0 and active[left] and not np.isfinite(result[left]):
                left -= 1
            right = i + 1
            while right < n and active[right] and not np.isfinite(result[right]):
                right += 1
            if (
                left >= 0
                and right < n
                and active[left : right + 1].all()
                and (right - left - 1) * 50 <= 150
            ):
                result[i] = result[left] + (result[right] - result[left]) * (i - left) / (
                    right - left
                )
        complete = np.isfinite(result[active]).all()
        heights = np.clip(result - bottom, 0, maximum - bottom)
        ratio = (
            float(np.sum(heights[active]) / np.sum((maximum - bottom)[active]))
            if complete
            else None
        )
        q = (
            (valid / eligible if eligible else 0)
            * coverage
            * min(1, frames[-1].scan_hz / self.target_hz)
            * (float(np.mean(stability)) if stability else 0)
        )
        finite = heights[observed]
        return (
            ratio,
            q,
            dict(
                valid_sample_ratio=valid / eligible if eligible else 0,
                coverage_ratio=coverage,
                **(
                    {
                        "median_height_mm": float(np.median(finite)),
                        "p90_height_mm": float(np.percentile(finite, 90)),
                    }
                    if len(finite)
                    else {}
                ),
            ),
            occlusion,
        )

    def measure(self, now_monotonic_ns, now_unix_ms, clock, measurement_id=None, cycle_id=None):
        if self.blocked:
            self.hint_baseline = None
            return None
        if self.hint_version != self.config["calibration"]["version"]:
            self.hint_baseline = None
            self.hint_version = self.config["calibration"]["version"]
        selected = {}
        reasons = []
        for sid, buf in self.buffers.items():
            recent = []
            historical = self.validated_history[sid]
            for f in buf:
                if not self._time_valid(f, now_monotonic_ns, now_unix_ms):
                    if f.acquired_monotonic_ns > now_monotonic_ns:
                        self.frame_loss += 1
                    continue
                if (f.instance_id, f.sequence) in historical:
                    recent.append(f)
                    continue
                old = self.last.get(sid)
                if old and old.instance_id == f.instance_id:
                    if (
                        f.sequence <= old.sequence
                        or f.acquired_monotonic_ns <= old.acquired_monotonic_ns
                    ):
                        self.frame_loss += 1
                        continue
                    self.frame_loss += max(0, f.sequence - old.sequence - 1)
                self.last[sid] = f
                recent.append(f)
            self.buffers[sid] = deque(recent, maxlen=10)
            # Only records validated in a prior cycle qualify as profile history.
            # New candidates are checked in arrival order above, never sorted or
            # reclassified as history merely because their sequence is lower.
            self.validated_history[sid] = {(f.instance_id, f.sequence) for f in recent}
            if recent:
                selected[sid] = recent
        if len(selected) > 1:
            newest = max(fs[-1].acquired_monotonic_ns for fs in selected.values())
            selected = {
                sid: [f for f in fs if newest - f.acquired_monotonic_ns <= 500_000_000]
                for sid, fs in selected.items()
            }
            selected = {sid: fs for sid, fs in selected.items() if fs}
        signature = tuple(
            (sid, fs[-1].instance_id, fs[-1].sequence) for sid, fs in selected.items()
        )
        if signature == self.last_signature:
            return None
        if len(selected) < len(self.sensors) and selected:
            if self.pair_wait_started is None:
                self.pair_wait_started = min(
                    fs[0].acquired_monotonic_ns for fs in selected.values()
                )
            if now_monotonic_ns - self.pair_wait_started < 500_000_000:
                return None
        else:
            self.pair_wait_started = None
        self.last_signature = signature
        outputs = []
        usable = []
        occlusion = False
        for sid in self.sensors:
            fs = selected.get(sid)
            if not fs:
                outputs.append(
                    dict(
                        sensor_id=sid,
                        sequence=self.last[sid].sequence if sid in self.last else 0,
                        valid_sample_ratio=0.0,
                        coverage_ratio=0.0,
                        state="INVALID",
                    )
                )
                reasons.append("SENSOR_ABSENT")
                continue
            f = fs[-1]
            ratio, q, metrics, sensor_occlusion = self._profile(sid, fs)
            occlusion |= sensor_occlusion
            key = (f.instance_id, f.sequence)
            if self.used.get(sid) == key:
                q = 0
                reasons.append("STALE_FRAME")
            self.used[sid] = key
            state = "GOOD" if q >= 0.7 else "DEGRADED" if q >= 0.4 else "INVALID"
            if ratio is None:
                state = "INVALID"
                q = 0
                reasons.append("INCOMPLETE_PROFILE")
            outputs.append(
                dict(
                    sensor_id=sid,
                    sequence=f.sequence,
                    instance_id=f.instance_id,
                    state=state,
                    **metrics,
                    **(
                        {"section_fill_ratio": ratio}
                        if ratio is not None and state != "INVALID"
                        else {}
                    ),
                )
            )
            if state != "INVALID":
                usable.append((sid, ratio, q))
        confidence = sum(self.sensors[s]["base_weight"] * q for s, _, q in usable)
        state = "INVALID"
        fill = None
        if len(usable) == len(self.sensors):
            ratio = sum(self.sensors[s]["base_weight"] * q * v for s, v, q in usable) / confidence
            fill = float(np.interp(ratio, self.fusion_map[:, 0], self.fusion_map[:, 1]))
            state = "GOOD" if all(s["state"] == "GOOD" for s in outputs) else "DEGRADED"
            if max(v for _, v, _ in usable) - min(v for _, v, _ in usable) > 0.2:
                state = "DEGRADED"
                confidence *= 0.7
                reasons.append("SENSOR_DISAGREEMENT")
        elif len(usable) == 1 and usable[0][0] in self.single_maps:
            sid, ratio, q = usable[0]
            m = self.single_maps[sid]
            fill = float(np.interp(ratio, m[:, 0], m[:, 1]))
            state = "DEGRADED"
            reasons.append("SINGLE_SENSOR_FALLBACK")
        else:
            reasons.append("INSUFFICIENT_SENSORS")
        if state == "INVALID":
            self.hint_baseline = None
        elif self.hints:
            coverage = {item["sensor_id"]: item["coverage_ratio"] for item in outputs}
            previous = self.hint_baseline
            if (
                previous
                and 0 < now_monotonic_ns - previous[0] <= self.hints["window_seconds"] * 1e9
            ):
                change = fill - previous[1]
                if change >= self.hints["rapid_rise_ratio"]:
                    reasons.append("RAPID_RISE_SUSPECTED")
                if -change >= self.hints["collection_drop_ratio"]:
                    reasons.append("COLLECTION_DROP_SUSPECTED")
                if any(
                    previous[2].get(sid, 0) - value >= self.hints["coverage_drop_ratio"]
                    for sid, value in coverage.items()
                ):
                    occlusion = True
            if occlusion:
                reasons.append("OCCLUSION_SUSPECTED")
                state = "DEGRADED"
            self.hint_baseline = (now_monotonic_ns, fill, coverage)
        m = dict(
            schema_version="1.0",
            measurement_id=measurement_id or str(uuid.uuid4()),
            measurement_cycle_id=cycle_id or str(uuid.uuid4()),
            site_id=self.config["site_id"],
            edge_id=self.config["edge_id"],
            measured_at=utc(now_unix_ms),
            calibration_version=self.config["calibration"]["version"],
            config_revision=self.config["config_revision"],
            sensors=outputs,
        )
        offset = clock.get("offset_ms")
        cs = clock.get("state", "UNSYNCED")
        window = (
            2000
            if cs == "SYNCED" and offset is not None and abs(offset) <= 100
            else 3000
            if cs in ("SYNCED", "DEGRADED") and offset is not None and abs(offset) <= 500
            else None
        )
        if window == 3000:
            reasons.append("CLOCK_DEGRADED")
        if window is None:
            reasons.append("CLOCK_UNSYNCED")
        if window and self.config.get("camera_id"):
            m["camera_reference"] = dict(
                camera_id=self.config["camera_id"],
                window_start=utc(now_unix_ms - window),
                window_end=utc(now_unix_ms + window),
            )
        m["quality"] = dict(
            state=state, confidence=max(0, min(1, confidence)), reason_codes=sorted(set(reasons))
        )
        if state != "INVALID":
            m.update(fill_ratio=fill, fill_percent=fill * 100)
        return m
