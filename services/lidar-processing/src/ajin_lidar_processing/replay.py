"""Streaming, deterministic ScanFrame replay without SDK, network or storage writes."""

import argparse
import json
from uuid import NAMESPACE_URL, uuid5

from ajin_edge.config import load_config
from ajin_edge.contracts import canonical_json, validate_measurement
from ajin_edge.wire.lidar_pb2 import ScanFrame
from google.protobuf.json_format import ParseDict

from .engine import ProcessingEngine


def replay_records(records, config):
    engine = ProcessingEngine(config)
    namespace = uuid5(NAMESPACE_URL, canonical_json(config).decode())
    for index, record in enumerate(records):
        if record.get("type") == "scan":
            engine.ingest(ParseDict(record["frame"], ScanFrame()))
        elif record.get("type") == "tick":
            result = engine.measure(
                record["monotonic_ns"],
                record["unix_ms"],
                record.get("clock", {"state": "UNSYNCED", "offset_ms": None}),
                measurement_id=str(uuid5(namespace, f"measurement-{index}")),
                cycle_id=str(uuid5(namespace, f"cycle-{index}")),
            )
            if result is not None:
                validate_measurement(result)
                yield result
        else:
            raise ValueError("replay record type must be scan or tick")


def read_records(path):
    with open(path, encoding="utf-8") as stream:
        while line := stream.readline(4 * 1024 * 1024 + 1):
            if len(line) > 4 * 1024 * 1024:
                raise ValueError("replay record exceeds 4 MiB")
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--allow-demo", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.allow_demo:
            config["allow_demo_calibration"] = True
        for result in replay_records(read_records(args.input), config):
            print(canonical_json(result).decode())
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
