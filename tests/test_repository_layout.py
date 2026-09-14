"""Repository packaging boundaries must survive directory reorganization."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_service_source_and_dockerfiles_are_colocated():
    expected = {
        "lidar-driver": "src/main.cpp",
        "lidar-processing": "src/ajin_lidar_processing/runtime.py",
        "measurement-uplink": "src/ajin_measurement_uplink/runtime.py",
        "edge-orchestrator": "src/ajin_edge_orchestrator/runtime.py",
        "camera-edge": "src/camera_edge/main.py",
    }
    for service, source in expected.items():
        assert (ROOT / "services" / service / source).is_file()
        assert (ROOT / "services" / service / "Dockerfile").is_file()


def test_wheel_packages_and_local_build_inputs_exist():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert len(packages) == 4
    for package in packages:
        assert (ROOT / package / "__init__.py").is_file()
    assert (ROOT / "packages/edge-common/src/ajin_edge/transport.py").is_file()
    assert (ROOT / "contracts/lidar/v1/lidar.proto").is_file()
    assert (ROOT / "deploy/compose.edge.example.yaml").is_file()
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert set(sdist["only-include"]) == set(packages) | {"pyproject.toml", "uv.lock", "README.md"}


def test_repository_docs_and_camera_ci_live_at_repository_root():
    assert (ROOT / "docs/ARCHITECTURE.md").is_file()
    assert (ROOT / "docs/OPERATIONS.md").is_file()
    camera_ci = ROOT / ".github/workflows/camera-ci.yml"
    assert "working-directory: services/camera-edge" in camera_ci.read_text()
    assert not (ROOT / "services/camera-edge/.github/workflows/ci.yml").exists()
    assert (ROOT / "services/camera-edge/.dockerignore").is_file()
