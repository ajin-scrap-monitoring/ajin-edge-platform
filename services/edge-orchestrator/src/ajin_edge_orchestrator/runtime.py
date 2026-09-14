import argparse
import asyncio
import time
from pathlib import Path

import httpx
from ajin_edge.clock import read_clock
from ajin_edge.config import load_runtime_config
from ajin_edge.lifecycle import Watchdog, supervise, termination_event
from ajin_edge.status import StatusWriter, read_status
from ajin_edge.transport import DeliveryClient, RetryBackoff, read_token

from .aggregate import EXPECTED_SERVICES, aggregate, meaningful_signature


async def heartbeat_loop(config, status_dir, clock_file, writer, transport):
    last_signature, last_sent, next_attempt = None, 0.0, 0.0
    backoff = RetryBackoff()
    fatal = False
    while True:
        snapshot = aggregate(
            config,
            [read_status(Path(status_dir) / name / f"{name}.json") for name in EXPECTED_SERVICES],
            read_clock(clock_file),
        )
        signature = meaningful_signature(snapshot)
        now = time.monotonic()
        if (
            not fatal
            and now >= next_attempt
            and (signature != last_signature or now - last_sent >= 10)
        ):
            result = await transport.send(snapshot, heartbeat=True)
            if result.kind == "accepted":
                last_signature, last_sent = signature, now
                backoff.success(now=now)
                writer.write("HEALTHY", aggregate_state=snapshot["state"])
            else:
                fatal = result.kind in ("fatal", "quarantine")
                next_attempt = now + max(backoff.failure(), result.retry_after)
                writer.write(
                    "FATAL" if fatal else "RETRYING",
                    [result.reason],
                    aggregate_state=snapshot["state"],
                )
        else:
            writer.write(
                "FATAL" if fatal else "RETRYING" if now < next_attempt else "HEALTHY",
                ["HEARTBEAT_HALTED"] if fatal else [],
                aggregate_state=snapshot["state"],
            )
        # Only the latest snapshot is retained; no heartbeat outbox.
        await asyncio.sleep(1)


async def serve(args):
    config = load_runtime_config(args.config, args.own_status_dir, "edge-orchestrator")
    writer = StatusWriter(
        args.own_status_dir,
        "edge-orchestrator",
        config["edge_id"],
        config["config_revision"],
        site_id=config["site_id"],
        deployment_revision=config.get("deployment_revision"),
    )
    writer.write("STARTING")
    with Watchdog() as watchdog:
        async with httpx.AsyncClient(trust_env=False) as http:
            transport = DeliveryClient(args.url, read_token(args.token_file), client=http)
            await supervise(
                termination_event(),
                watchdog.pulse(),
                heartbeat_loop(config, args.status_dir, args.clock_file, writer, transport),
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--status-dir", default="/run/ajin-edge/status")
    parser.add_argument("--own-status-dir", default="/run/ajin-edge/orchestrator-status")
    parser.add_argument("--clock-file", default="/run/ajin-edge/clock/clock.json")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    asyncio.run(serve(parser.parse_args()))
