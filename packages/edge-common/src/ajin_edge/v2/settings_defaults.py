"""Explicit simulator defaults; never infer a field installation from missing settings."""

from .processing import validate_configuration

PROFILE_ID = "synthetic-scrap-pit-v1"


def default_settings(configuration):
    if configuration["coordinate_frame"]["frame_id"] != PROFILE_ID or {
        s["sensor_id"] for s in configuration["sensors"]
    } != {"lidar-a", "lidar-b"}:
        raise ValueError("no built-in defaults for this installation")
    poses = [
        (
            "lidar-a",
            [500, 2500, 10000],
            [
                [0, -1, 0],
                [-0.22414769292142694, 0, 0.9745551866148997],
                [-0.9745551866148997, 0, -0.22414769292142694],
            ],
            [1000, 0],
            [1, 0],
        ),
        (
            "lidar-b",
            [2300, 3900, 10000],
            [
                [0.14834045293024462, 0, -0.9889363528682975],
                [0, -1, 0],
                [-0.9889363528682975, 0, -0.14834045293024462],
            ],
            [0, 1000],
            [0, 1],
        ),
    ]
    calibrations, sections = [], []
    for sid, translation, rotation, origin, axis in poses:
        calibrations.append(
            {
                "sensor_id": sid,
                "calibration": {
                    "version": "default-simulator-installation-v1",
                    "verification_state": "VERIFIED",
                    "angle_sign": 1,
                    "angle_zero_offset_mdeg": 0,
                    "rotation": rotation,
                    "translation_mm": translation,
                },
            }
        )
        sections.append(
            {
                "sensor_id": sid,
                "origin_xy_mm": origin,
                "axis_xy": axis,
                "bin_width_mm": 250,
                "floor_z_mm": [0] * 4,
                "ceiling_z_mm": [10000] * 4,
                "active_bins": [True] * 4,
                "filters": {
                    "quality_min": 10,
                    "distance_min_mm": 1,
                    "distance_max_mm": 30000,
                    "height_margin_mm": 30,
                    "spatial_radius_bins": 1,
                    "spatial_max_deviation_mm": 200,
                },
                "max_interpolation_gap_bins": 1,
            }
        )
    judgment = {
        "model_type": "INDEPENDENT_SECTIONS_V1",
        "method_version": "default-interior-sections-v1",
        "verification_state": "DEFAULT",
        "sections": sections,
        "window": {"scan_count": 5, "min_scans": 3, "max_age_ms": 1000, "pair_tolerance_ms": 100},
        "fusion": {
            "method": "CALIBRATED_WEIGHTED_SECTIONS",
            "version": "default-equal-fusion-v1",
            "verification_state": "DEFAULT",
            "components": [
                {"sensor_id": sid, "weight": 0.5, "calibration": [[0, 0], [1, 1]]}
                for sid in ["lidar-a", "lidar-b"]
            ],
        },
    }
    sensors = {s["sensor_id"]: s for s in configuration["sensors"]}
    validate_configuration(
        {
            **configuration,
            "judgment": judgment,
            "sensors": [
                {**sensors[c["sensor_id"]], "calibration": c["calibration"]} for c in calibrations
            ],
        }
    )
    return {
        "profile_id": PROFILE_ID,
        "coordinate_frame_id": PROFILE_ID,
        "calibrations": calibrations,
        "judgment": judgment,
    }
