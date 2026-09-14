"""Create only an explicitly selected runtime directory tree, without secrets."""

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--uid", type=int, default=10001)
    parser.add_argument("--gid", type=int, default=10001)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if root == Path(root.anchor) or root == Path.home():
        raise ValueError("select a dedicated runtime directory")
    directories = [
        "sockets/a",
        "sockets/b",
        "sockets/uplink",
        "outbox",
        "clock",
        "status/lidar-driver-a",
        "status/lidar-driver-b",
        "status/lidar-processing",
        "status/measurement-uplink",
        "status/camera-edge",
        "orchestrator-status",
    ]
    for relative in directories:
        path = root / relative
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise ValueError(f"unsafe runtime path: {path}")
        if not (root / relative).resolve().is_relative_to(root):
            raise ValueError("runtime path escapes root")
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path, 0o750)
            if os.geteuid() == 0:
                os.chown(path, args.uid, args.gid)
    print(root)


if __name__ == "__main__":
    main()
