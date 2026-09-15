import importlib.util
import json
import sys
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / "tools/release_metadata.py"
    spec = importlib.util.spec_from_file_location("release_metadata", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_release_requires_all_five_images_with_same_source_and_version():
    release = module()
    rows = [
        {
            "service": service,
            "image": f"ghcr.io/ajin-scrap-monitoring/ajin-{service}",
            "digest": "sha256:" + "a" * 64,
            "revision": "b" * 40,
            "version": "v0.2.0",
        }
        for service in release.SERVICES
    ]
    result = release.assemble(rows, "v0.2.0", "b" * 40)
    assert len(result["images"]) == 5
    assert result["images"]["camera-edge"].endswith("@sha256:" + "a" * 64)
    for invalid in (
        rows[:-1],
        rows + rows[:1],
        [dict(r, revision="c" * 40) for r in rows],
        [dict(r, digest="latest") for r in rows],
        [dict(r, image="untrusted.example/image") for r in rows],
    ):
        with pytest.raises(ValueError):
            release.assemble(invalid, "v0.2.0", "b" * 40)


@pytest.mark.parametrize("tag", ["main", "v1.2", "v01.2.3", "v1.2.3;echo bad", "v1.2.3-rc1"])
def test_rejects_non_release_tags(tag):
    with pytest.raises(ValueError):
        module().validate_version(tag)


def test_cli_emits_all_compose_digest_variables(tmp_path, monkeypatch):
    release = module()
    inputs = tmp_path / "input"
    inputs.mkdir()
    for service in release.SERVICES:
        (inputs / f"{service}.json").write_text(
            json.dumps(
                {
                    "service": service,
                    "image": release.PREFIX + service,
                    "digest": "sha256:" + "a" * 64,
                    "revision": "b" * 40,
                    "version": "v0.2.0",
                }
            )
        )
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_metadata.py",
            "--version",
            "v0.2.0",
            "--revision",
            "b" * 40,
            "--input",
            str(inputs),
            "--output",
            str(output),
        ],
    )
    release.main()
    manifest = json.loads((output / "release-manifest.json").read_text())
    assert manifest["platform"] == "linux/arm64"
    variables = dict(
        line.split("=", 1) for line in (output / "images.env").read_text().splitlines()
    )
    assert len(variables) == 5
    for service in release.SERVICES:
        assert (
            variables[service.upper().replace("-", "_") + "_IMAGE"] == manifest["images"][service]
        )
