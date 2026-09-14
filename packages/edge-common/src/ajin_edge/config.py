"""Fail-closed shared configuration loading; algorithm validation lives in processing."""

import hashlib
import json
import os
import re
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def load_config(path):
    raw = Path(path).read_bytes()
    expected = os.environ.get("CONFIG_SHA256")
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("CONFIG_CHECKSUM_MISMATCH")
    config = json.loads(raw)
    if not isinstance(config, dict) or config.get("schema_version") != "1.0":
        raise ValueError("unsupported config schema")
    for key in ("site_id", "edge_id", "config_revision"):
        identifier(config.get(key))
    for key in ("camera_id", "deployment_revision"):
        if key in config:
            identifier(config[key])
    for key in ("site_id", "edge_id", "config_revision", "camera_id", "deployment_revision"):
        if key.upper() in os.environ and config.get(key) != identifier(os.environ[key.upper()]):
            raise ValueError("CONFIG_DEPLOYMENT_METADATA_MISMATCH")
    manifest = config.get("service_versions", {})
    if not isinstance(manifest, dict):
        raise ValueError("invalid service version manifest")
    for name, version in manifest.items():
        identifier(name)
        identifier(version)
    if "deployment_revision" in config and set(manifest) != {
        "lidar-driver-a",
        "lidar-driver-b",
        "lidar-processing",
        "measurement-uplink",
        "camera-edge",
        "edge-orchestrator",
    }:
        raise ValueError("complete six-service version manifest required")
    sensors = config.get("sensors")
    if not isinstance(sensors, list) or len(sensors) != 2:
        raise ValueError("exactly two sensor configurations required")
    if any(not isinstance(sensor, dict) for sensor in sensors):
        raise ValueError("invalid sensor configuration")
    ids = [identifier(sensor.get("sensor_id")) for sensor in sensors]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate sensor identity")
    return config


def deployment_metadata():
    """Only deployment environment can identify a rejected configuration."""
    return {
        key: identifier(os.environ.get(key.upper()))
        for key in ("site_id", "edge_id", "config_revision", "deployment_revision", "camera_id")
    }


def load_runtime_config(path, status_dir, service):
    from . import __version__
    from .status import StatusWriter

    try:
        config = load_config(path)
        expected = config.get("service_versions", {}).get(service)
        if expected is not None and expected != __version__:
            raise ValueError("CONFIG_SERVICE_VERSION_MISMATCH")
        return config
    except (ValueError, TypeError, KeyError, OSError) as error:
        try:
            metadata = deployment_metadata()
        except ValueError:
            # No rejected JSON identity is trusted as a fallback.
            raise error from None
        code = (
            str(error)
            if str(error)
            in (
                "CONFIG_CHECKSUM_MISMATCH",
                "CONFIG_SERVICE_VERSION_MISMATCH",
                "CONFIG_DEPLOYMENT_METADATA_MISMATCH",
            )
            else "CONFIG_INVALID"
        )
        StatusWriter(
            status_dir,
            service,
            metadata["edge_id"],
            metadata["config_revision"],
            site_id=metadata["site_id"],
            deployment_revision=metadata["deployment_revision"],
        ).write("FATAL", [code], camera_id=metadata["camera_id"])
        raise
