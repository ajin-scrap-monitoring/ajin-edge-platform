from ajin_edge.status import utc_now

EXPECTED_SERVICES = (
    "lidar-driver-a",
    "lidar-driver-b",
    "lidar-processing",
    "measurement-uplink",
    "camera-edge",
)


def aggregate(config, services, clock):
    by_name = {item["service"]: item for item in services}
    services = [
        by_name.get(name, {"service": name, "state": "UNKNOWN", "reason_codes": ["STATUS_MISSING"]})
        for name in EXPECTED_SERVICES
    ]
    by_name = {item["service"]: item for item in services}
    reasons = set()
    ready = {"HEALTHY", "DEGRADED"}
    mismatch = False
    for item in services:
        reasons.update(item.get("reason_codes", []))
        if item["state"] != "UNKNOWN":
            for key, code in [
                ("site_id", "CONFIG_SITE_MISMATCH"),
                ("deployment_revision", "CONFIG_DEPLOYMENT_MISMATCH"),
            ]:
                if key in config and item.get(key) != config[key]:
                    reasons.add(code)
            if (
                item["service"] == "camera-edge"
                and "camera_id" in config
                and item.get("camera_id") != config["camera_id"]
            ):
                reasons.add("CONFIG_CAMERA_MISMATCH")
            expected = config.get("service_versions", {}).get(item["service"])
            if expected is not None and item.get("service_version") != expected:
                reasons.add("CONFIG_SERVICE_VERSION_MISMATCH")
        if item["state"] != "UNKNOWN" and (
            item.get("edge_id") != config["edge_id"]
            or item.get("config_revision") != config["config_revision"]
        ):
            mismatch = True
    if clock["state"] != "SYNCED":
        reasons.add(f"CLOCK_{clock['state']}")
    if mismatch or any(code.startswith(("CONFIG_", "CALIBRATION_")) for code in reasons):
        state = "CONFIG_ERROR"
        reasons.add("CONFIG_REVISION_MISMATCH" if mismatch else "CONFIG_ERROR")
    elif by_name["lidar-processing"]["state"] not in ready or all(
        by_name[name]["state"] not in ready for name in EXPECTED_SERVICES[:2]
    ):
        state = "MEASUREMENT_UNAVAILABLE"
    elif by_name["measurement-uplink"]["state"] not in ready:
        state = "OFFLINE_BUFFERING"
    elif any(item["state"] != "HEALTHY" for item in services) or clock["state"] != "SYNCED":
        state = "DEGRADED"
    else:
        state = "HEALTHY"
    return {
        "schema_version": "1.0",
        "site_id": config["site_id"],
        "edge_id": config["edge_id"],
        "config_revision": config["config_revision"],
        **{key: config[key] for key in ("deployment_revision", "camera_id") if key in config},
        "reported_at": utc_now(),
        "state": state,
        "reason_codes": sorted(reasons),
        "clock": clock,
        "services": services,
    }


def meaningful_signature(heartbeat):
    return (
        heartbeat["state"],
        heartbeat["clock"]["state"],
        tuple(heartbeat["reason_codes"]),
        tuple(
            (
                item["service"],
                item["state"],
                item.get("instance_id"),
                item.get("config_revision"),
                item.get("site_id"),
                item.get("camera_id"),
                item.get("deployment_revision"),
                item.get("service_version"),
                tuple(item.get("reason_codes", [])),
            )
            for item in heartbeat["services"]
        ),
    )
