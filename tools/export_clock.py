"""Host-only clock snapshot exporter. Reads chrony; never changes system time."""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/edge-common/src"))

from ajin_edge.clock import probe_chrony  # noqa: E402
from ajin_edge.status import atomic_json, utc_now  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        result = probe_chrony()
        atomic_json(
            args.output,
            {
                **result,
                "synchronized": result["offset_ms"] is not None,
                "reported_at": utc_now(),
                "source": "chronyc tracking",
            },
        )
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
