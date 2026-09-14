import json
from pathlib import Path

from ajin_edge.contracts import HEARTBEAT_SCHEMA, MEASUREMENT_SCHEMA

target = Path(__file__).resolve().parents[1] / "contracts/backend/v1"
target.mkdir(parents=True, exist_ok=True)
for name, schema in (("measurement", MEASUREMENT_SCHEMA), ("edge-heartbeat", HEARTBEAT_SCHEMA)):
    (target / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
