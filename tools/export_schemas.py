import json
from pathlib import Path

from ajin_edge.contracts import HEARTBEAT_SCHEMA, MEASUREMENT_SCHEMA
from ajin_edge.v2.contracts import SCHEMAS
from ajin_edge.v2.transport import PATHS

target = Path(__file__).resolve().parents[1] / "contracts/backend/v1"
target.mkdir(parents=True, exist_ok=True)
for name, schema in (("measurement", MEASUREMENT_SCHEMA), ("edge-heartbeat", HEARTBEAT_SCHEMA)):
    (target / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")

target = target.parent / "v2"
target.mkdir(parents=True, exist_ok=True)
for kind, schema in SCHEMAS.items():
    (target / f"{PATHS[kind]}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")

ack = {
    "type": "object",
    "required": ["message_id", "accepted", "duplicate", "received_at"],
    "properties": {
        "message_id": {"type": "string"},
        "accepted": {"const": True},
        "duplicate": {"type": "boolean"},
        "received_at": {"type": "string", "format": "date-time"},
    },
}
paths = {}
for name in PATHS.values():
    paths[f"/api/edge/v2/{name}"] = {
        "post": {
            "operationId": f"receive_{name}",
            "security": [{"deviceToken": []}],
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 128},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"./{name}.schema.json"}}},
            },
            "responses": {
                "201": {
                    "description": "Durably stored new message; duplicate=false",
                    "content": {"application/json": {"schema": ack}},
                },
                "200": {
                    "description": "Identical message already stored; duplicate=true",
                    "content": {"application/json": {"schema": ack}},
                },
                "409": {"description": "IDEMPOTENCY_CONFLICT; never duplicate success"},
                "400": {"description": "Invalid message"},
                "401": {"description": "Authentication failed"},
                "403": {"description": "Device/site authorization failed"},
                "413": {"description": "Payload rejected; preserve original in quarantine"},
                "422": {"description": "Semantic validation failed"},
                "429": {"description": "Retry after server-specified delay"},
                "503": {"description": "Temporary failure; retry identical message"},
            },
        }
    }
(target / "openapi.json").write_text(
    json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "Edge backend ingestion", "version": "2.0"},
            "paths": paths,
            "components": {
                "securitySchemes": {"deviceToken": {"type": "http", "scheme": "bearer"}}
            },
        },
        indent=2,
    )
    + "\n"
)
