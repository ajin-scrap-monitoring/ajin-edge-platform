"""Generate both Python contracts using the locked grpcio-tools dependency."""

from pathlib import Path

from grpc_tools import protoc

root = Path(__file__).resolve().parents[1]
source = root / "contracts/lidar/v1"
target = root / "packages/edge-common/src/ajin_edge/wire"
for name in ("lidar", "delivery"):
    result = protoc.main(
        [
            "protoc",
            f"-I{source}",
            f"--python_out={target}",
            f"--grpc_python_out={target}",
            str(source / f"{name}.proto"),
        ]
    )
    if result:
        raise SystemExit(result)
    stub = target / f"{name}_pb2_grpc.py"
    stub.write_text(
        stub.read_text().replace(f"import {name}_pb2 as", f"from . import {name}_pb2 as")
    )
