"""One bounded budget for telemetry plus the separately mounted control directory."""

from pathlib import Path

STATUS_RESERVE = 1024 * 1024


def allocated_bytes(directory):
    root = Path(directory)
    if not root.exists():
        return 0
    total = 0
    for path in root.iterdir():
        try:
            stat = path.stat()
            if path.is_file():
                total += max(
                    ((stat.st_size + 4095) // 4096) * 4096, getattr(stat, "st_blocks", 0) * 512
                )
        except FileNotFoundError:
            continue
    return total


def auxiliary_bytes(config):
    total = allocated_bytes(config["status_dir"])
    if "control_dir" in config:
        total += allocated_bytes(config["control_dir"])
    return total
