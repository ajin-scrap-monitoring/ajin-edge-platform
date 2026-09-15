"""Validate a complete release and emit digest-pinned deployment references."""

import argparse
import json
import re
from pathlib import Path

SERVICES = (
    "lidar-driver",
    "lidar-processing",
    "measurement-uplink",
    "edge-orchestrator",
    "camera-edge",
)
PREFIX = "ghcr.io/ajin-scrap-monitoring/ajin-"


def validate_version(version):
    if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("release tag must be vMAJOR.MINOR.PATCH")


def assemble(rows, version, revision):
    validate_version(version)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("full source revision required")
    images = {}
    for row in rows:
        service = row["service"]
        if service not in SERVICES or service in images:
            raise ValueError("unknown or duplicate service")
        if row["revision"] != revision or row["version"] != version:
            raise ValueError("mixed release revisions or versions")
        if row["image"] != PREFIX + service:
            raise ValueError("unexpected image registry or name")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"]):
            raise ValueError("immutable digest required")
        images[service] = row["image"] + "@" + row["digest"]
    if set(images) != set(SERVICES):
        raise ValueError("all five images must succeed")
    return {"version": version, "revision": revision, "platform": "linux/arm64", "images": images}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(path.read_text()) for path in sorted(args.input.glob("*.json"))]
    result = assemble(rows, args.version, args.revision)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "release-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    references = [f"{s.upper().replace('-', '_')}_IMAGE={result['images'][s]}" for s in SERVICES]
    (args.output / "images.env").write_text("\n".join(references) + "\n")


if __name__ == "__main__":
    main()
