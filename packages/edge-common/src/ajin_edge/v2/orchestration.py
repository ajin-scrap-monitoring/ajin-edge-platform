"""Observation health and independent, explicitly configured section judgments."""

import time
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from . import point_cloud
from .contracts import validate_message
from .judgment import SectionJudgment, fill_enabled, is_independent
from .loss_recovery import OutboxLossRecovery
from .processing import canonical, copy_observation, envelope, utc_now, validate_configuration


class ConfigurationGate:
    def __init__(self):
        self.active = None
        self.pending = None
        self.revisions = {}
        self.installation_invalid = False

    def propose(self, request):
        validate_configuration(request)
        revision = request["config_revision"]
        content = canonical(
            {
                key: value
                for key, value in request.items()
                if key not in ("message_id", "generated_at")
            }
        )
        if revision in self.revisions and self.revisions[revision] != content:
            raise ValueError("configuration revision is immutable")
        if revision not in self.revisions and len(self.revisions) >= 64:
            raise ValueError("configuration revision registry requires archival")
        self.revisions[revision] = content
        self.pending = deepcopy(request)

    def acknowledge(self, message_id):
        # Caller must supply only IDs from validated remote storage ACKs, not local enqueue.
        if self.pending is None or self.pending["message_id"] != message_id:
            return False
        self.active, self.pending = self.pending, None
        self.installation_invalid = False
        return True

    def invalidate_installation(self):
        self.installation_invalid = True

    def effective(self):
        config = deepcopy(self.active)
        if config and self.installation_invalid:
            for sensor in config["sensors"]:
                if sensor["calibration"]:
                    sensor["calibration"]["verification_state"] = "UNVERIFIED"
        return config


class Orchestrator:
    def __init__(self, config, *, enabled_rules=()):
        validate_configuration(config)
        self.config = deepcopy(config)
        self.instance_id = str(uuid4())
        self.sequence = 0
        self.sensors = {
            s["sensor_id"]: {
                "scan_id": None,
                "at": None,
                "ns": None,
                "recovering_since": None,
                "stalled": False,
            }
            for s in config["sensors"]
        }
        self.enabled_rules = frozenset(enabled_rules)
        self.events = {}
        self.observations = 0
        self.judgment = SectionJudgment(self.config)
        self.loss_recovery = OutboxLossRecovery()
        self.recovery_evidence = None

    def ingest(self, observation, *, now_ns):
        sensor = self.sensors[observation["sensor_id"]]
        scan = observation["scan"]
        if scan is None or scan["scan_id"] == sensor["scan_id"]:
            return False
        acquired = int(scan["acquired_monotonic_ns"])
        if acquired > now_ns or (sensor["ns"] is not None and acquired <= sensor["ns"]):
            return False
        self._state(sensor, now_ns)
        if sensor["stalled"]:
            sensor["recovering_since"] = acquired
            sensor["stalled"] = False
        sensor.update(scan_id=scan["scan_id"], at=scan["acquired_at"], ns=acquired)
        self._state(sensor, now_ns)
        self.observations += 1
        self.judgment.ingest(observation)
        return True

    @staticmethod
    def _state(sensor, now_ns):
        if sensor["ns"] is None:
            return "UNAVAILABLE"
        if now_ns - sensor["ns"] > 1000000000:
            sensor["stalled"] = True
            sensor["recovering_since"] = None
            return "STALE"
        start = sensor["recovering_since"]
        if start is not None:
            # New observations must themselves span the complete recovery interval.
            if sensor["ns"] - start < 1000000000:
                return "RECOVERING"
            sensor["recovering_since"] = None
        return "FRESH"

    def sensor_states(self, *, now_ns):
        result = []
        for sensor_id, sensor in self.sensors.items():
            state = self._state(sensor, now_ns)
            result.append(
                {
                    "sensor_id": sensor_id,
                    "data_state": state,
                    "last_scan_at": sensor["at"],
                    "reason_codes": ["SCAN_NOT_UPDATED"]
                    if state in ("STALE", "UNAVAILABLE")
                    else ["SCAN_RECOVERING"]
                    if state == "RECOVERING"
                    else [],
                }
            )
        return result

    def _empty_assessment(self):
        # Without a rule there are no "used" scans; input is not evidence of a calculation.
        model = self.config.get("judgment")
        enabled = fill_enabled(model)
        return {
            **envelope(self.config, "edge_assessment"),
            "assessment_type": "STATE",
            "assessment_id": str(uuid4()),
            "revision": 1,
            "observed_from": None,
            "observed_to": None,
            "rule_version": None,
            "decision_state": "UNAVAILABLE",
            "reason_codes": ["FILL_METHOD_NOT_CONFIGURED"],
            "fill_state": "UNAVAILABLE",
            "fill_estimate": None,
            "event": None,
            "capabilities": {
                "fill": "ENABLED" if enabled else "NOT_VALIDATED",
                **{k: "NOT_IMPLEMENTED" for k in ("occlusion", "collection", "vision")},
            },
            "evidence": {"scans": [], "vision_observations": []},
        }

    def _evaluate(self, *, now_ns=None, generated_at=None):
        fixed_clock = now_ns is not None
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        body = self._empty_assessment()
        model = self.config.get("judgment")
        enabled = fill_enabled(model)
        body["capabilities"]["fill"] = "ENABLED" if enabled else "NOT_VALIDATED"
        cloud_enabled = is_independent(model) and point_cloud.enabled(model)
        histories = self.judgment.snapshot(now_ns) if cloud_enabled else None
        profile, reasons, used = (
            self.judgment.calculate(now_ns, histories=histories)
            if cloud_enabled
            else self.judgment.calculate(now_ns)
        )
        if (
            used
            and not fixed_clock
            and any(
                not 0
                <= time.monotonic_ns() - int(o["scan"]["acquired_monotonic_ns"])
                <= model["window"]["max_age_ms"] * 1000000
                for o in used
            )
        ):
            profile, reasons, used = None, ["INPUT_EXPIRED_DURING_JUDGMENT"], []
        body["generated_at"] = generated_at or utc_now()
        body["reason_codes"] = reasons
        if is_independent(model):
            body["section_profiles"] = profile["section_profiles"] if profile else None
            body["fusion_result"] = profile["fusion_result"] if profile else None
        if model:
            body["rule_version"] = model["method_version"]
        if used:
            stamps = [o["scan"]["acquired_at"] for o in used]
            body.update(
                observed_from=min(stamps, key=datetime.fromisoformat),
                observed_to=max(stamps, key=datetime.fromisoformat),
            )
            body["evidence"]["scans"] = [self.reference(o) for o in used]
        if profile:
            if any(r["state"] != "UNAVAILABLE" for r in profile["section_profiles"]):
                body["decision_state"] = "LIMITED"
            fusion = profile["fusion_result"]
            if fusion is not None:
                body["decision_state"] = "LIMITED" if reasons else "AVAILABLE"
                body["fill_state"] = "AVAILABLE"
                body["fill_estimate"] = {
                    "ratio": fusion["ratio"],
                    "method_version": fusion["model_version"],
                }
        if cloud_enabled:
            clouds = None
            if profile is not None:
                clouds = []
                by_reference = {tuple(self.reference(o).values()): o for o in used}
                for sensor in self.config["sensors"]:
                    history = histories[sensor["sensor_id"]]
                    cloud = point_cloud.build_cloud(self.config, sensor, history, now_ns=now_ns)
                    clouds.append(cloud)
                    required_ids = {r["scan_id"] for r in cloud["source_scans"]}
                    for o in history:
                        if o["scan"]["scan_id"] in required_ids:
                            by_reference[tuple(self.reference(o).values())] = o
                if not fixed_clock:
                    completed_ns = time.monotonic_ns()
                    for cloud in clouds:
                        if any(
                            completed_ns - int(o["scan"]["acquired_monotonic_ns"])
                            > model["window"]["max_age_ms"] * 1000000
                            for o in histories[cloud["sensor_id"]]
                        ):
                            point_cloud.invalidate_expired(cloud)
                used = list(by_reference.values())
                if used:
                    stamps = [o["scan"]["acquired_at"] for o in used]
                    body["observed_from"] = min(stamps, key=datetime.fromisoformat)
                    body["observed_to"] = max(stamps, key=datetime.fromisoformat)
                    body["evidence"]["scans"] = [self.reference(o) for o in used]
            body["filtered_point_clouds"] = clouds
        validate_message(body)
        return body, used

    def assessment(self, *, now_ns=None, generated_at=None):
        return self._evaluate(now_ns=now_ns, generated_at=generated_at)[0]

    def assessment_bundle(self, *, now_ns=None, generated_at=None):
        body, used = self._evaluate(now_ns=now_ns, generated_at=generated_at)
        self.recovery_evidence = (
            time.monotonic_ns() if now_ns is None else now_ns,
            frozenset(r["scan_id"] for r in body["evidence"]["scans"]),
        )
        return [self.evidence(o, [body["assessment_id"]]) for o in used] + [body]

    def event(self, kind, stage, used_observations, *, rule_version, assessment_id=None):
        if rule_version is None or rule_version not in self.enabled_rules:
            raise ValueError("only explicitly validated rules can emit events")
        if not used_observations or any(o["scan"] is None for o in used_observations):
            raise ValueError("actual used scan evidence required")
        if assessment_id is None:
            if len(self.events) >= 128:
                raise ValueError("active event capacity exceeded")
            assessment_id = str(uuid4())
            revision = 1
        else:
            previous = self.events[assessment_id]
            allowed = {
                "SUSPECTED": {"CONFIRMED", "RESOLVED", "RETRACTED"},
                "CONFIRMED": {"RESOLVED", "RETRACTED"},
            }
            if previous["kind"] != kind or stage not in allowed.get(previous["stage"], set()):
                raise ValueError("invalid event transition")
            revision = previous["revision"] + 1
        payload = self._empty_assessment()
        stamps = [o["scan"]["acquired_at"] for o in used_observations]
        from datetime import datetime

        payload.update(
            assessment_type="EVENT",
            assessment_id=assessment_id,
            revision=revision,
            observed_from=min(stamps, key=datetime.fromisoformat),
            observed_to=max(stamps, key=datetime.fromisoformat),
            rule_version=rule_version,
            decision_state="AVAILABLE",
            reason_codes=[],
            event={"kind": kind, "stage": stage},
        )
        payload["evidence"]["scans"] = [self.reference(o) for o in used_observations]
        validate_message(payload)
        self.events[assessment_id] = {"kind": kind, "stage": stage, "revision": revision}
        if stage in ("RESOLVED", "RETRACTED"):
            del self.events[assessment_id]
        return payload

    def reference(self, observation):
        return {
            "scan_id": observation["scan"]["scan_id"],
            "sensor_id": observation["sensor_id"],
            "config_revision": self.config["config_revision"],
            "calibration_version": observation["calibration_version"],
            "coordinate_frame_id": self.config["coordinate_frame"]["frame_id"],
        }

    def evidence(self, observation, assessment_ids):
        payload = {
            **envelope(self.config, "lidar_measurement"),
            "delivery_kind": "EVIDENCE",
            "report_interval_ms": None,
            "selection_policy": "REFERENCED_SCANS",
            "coordinate_frame_id": self.config["coordinate_frame"]["frame_id"],
            "pair_state": "NOT_APPLICABLE",
            "pair_delta_ms": None,
            "sensors": [copy_observation(observation)],
            "assessment_ids": list(assessment_ids),
        }
        validate_message(payload)
        return payload

    def status(self, stats, services, *, now_ns, clock=None, recovery_context=None):
        self.sequence += 1
        sensors = self.sensor_states(now_ns=now_ns)
        states = {s["data_state"] for s in sensors}
        reasons = sorted({r for s in sensors for r in s["reason_codes"]})
        service_problem = any(s["state"] != "HEALTHY" for s in services)
        context = recovery_context or {"healthy": False, "epoch": None}
        required = {"lidar-processing", "measurement-uplink", "edge-orchestrator"} | {
            "lidar-driver-" + s["sensor_id"].removeprefix("lidar-") for s in self.config["sensors"]
        }
        by_name = {s["service"]: s for s in services}
        services_healthy = all(by_name.get(name, {}).get("state") == "HEALTHY" for name in required)
        evidence = self.recovery_evidence
        evidence_known = evidence is not None and 0 <= now_ns - evidence[0] <= 15_000_000_000
        current_ids = {s["scan_id"] for s in self.sensors.values() if s["scan_id"]}
        if evidence_known:
            current_ids.update(evidence[1])
        missing = bool(current_ids.intersection(stats["unavailable_evidence_scan_ids"]))
        loss_active = self.loss_recovery.observe(
            stats["lost_messages"],
            healthy=states == {"FRESH"}
            and services_healthy
            and context["healthy"]
            and evidence_known,
            epoch=context["epoch"],
            now=now_ns / 1_000_000_000,
            evidence_missing=missing,
        )
        overall = (
            "MEASUREMENT_UNAVAILABLE"
            if states <= {"STALE", "UNAVAILABLE"}
            else (
                "DEGRADED" if states != {"FRESH"} or service_problem or loss_active else "HEALTHY"
            )
        )
        if loss_active:
            reasons.append("OUTBOX_DATA_LOSS")
        return {
            **envelope(self.config, "edge_status"),
            "orchestrator_instance_id": self.instance_id,
            "sequence": self.sequence,
            "overall_state": overall,
            "reason_codes": reasons,
            "clock": clock or {"state": "UNSYNCED", "offset_ms": None},
            "sensors": sensors,
            "services": services,
            "delivery": {
                k: stats[k]
                for k in (
                    "pending_messages",
                    "pending_bytes",
                    "quarantined_messages",
                    "last_backend_ack_at",
                )
            },
            "storage": {
                k: stats[k]
                for k in (
                    "limit_bytes",
                    "used_bytes",
                    "lost_messages",
                    "loss_from",
                    "loss_to",
                    "last_loss_reason",
                    "unavailable_evidence_scan_ids",
                )
            },
        }
