"""Pi control requests; distinct from backend configuration ingestion."""

from copy import deepcopy

from .contracts import CONFIGURATION_SCHEMA, ID, array, obj

CALIBRATION = deepcopy(
    CONFIGURATION_SCHEMA["properties"]["sensors"]["items"]["properties"]["calibration"]["anyOf"][0]
)
CALIBRATION["properties"]["translation_mm"] = array(
    {"type": "number", "minimum": -1e15, "maximum": 1e15}, minItems=3, maxItems=3
)
CALIBRATION["properties"]["rotation"] = array(
    array(
        {"type": "number", "minimum": -1.00000001, "maximum": 1.00000001}, minItems=3, maxItems=3
    ),
    minItems=3,
    maxItems=3,
)
CALIBRATION_REQUEST_SCHEMA = obj(
    {
        "site_id": ID,
        "edge_id": ID,
        "base_config_revision": ID,
        "config_revision": ID,
        "coordinate_frame_id": ID,
        "calibrations": array(
            obj({"sensor_id": ID, "calibration": CALIBRATION}), minItems=1, maxItems=2
        ),
    }
)
