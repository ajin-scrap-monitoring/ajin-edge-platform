"""Independent local measurement sink and bounded backend delivery worker."""

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import grpc
import httpx
from ajin_edge.config import load_runtime_config
from ajin_edge.lifecycle import Watchdog, supervise, termination_event
from ajin_edge.status import StatusWriter
from ajin_edge.transport import DeliveryClient, RetryBackoff, read_token
from ajin_edge.wire import delivery_pb2, delivery_pb2_grpc

from .outbox import DuplicateConflict, Outbox


class MeasurementSink(delivery_pb2_grpc.MeasurementSinkServicer):
    def __init__(self, outbox, *, edge_id, config_revision):
        self.outbox, self.edge_id, self.revision = outbox, edge_id, config_revision

    async def Enqueue(self, request, context):
        if len(request.measurement_json) > 65536:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "payload too large")
        try:
            payload = json.loads(request.measurement_json)
            if not isinstance(payload, dict):
                raise ValueError("expected envelope")
            if (
                payload.get("edge_id") != self.edge_id
                or payload.get("config_revision") != self.revision
            ):
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "CONFIG_REVISION_MISMATCH")
            duplicate = self.outbox.enqueue(payload)
            return delivery_pb2.EnqueueReply(
                measurement_id=payload["measurement_id"], duplicate=duplicate
            )
        except DuplicateConflict:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "conflicting measurement ID")
        except (ValueError, TypeError, KeyError):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid measurement")
        except sqlite3.Error:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "outbox unavailable")


async def make_server(outbox, endpoint, *, edge_id, config_revision):
    server = grpc.aio.server(
        options=[("grpc.max_receive_message_length", 66000), ("grpc.max_send_message_length", 4096)]
    )
    delivery_pb2_grpc.add_MeasurementSinkServicer_to_server(
        MeasurementSink(outbox, edge_id=edge_id, config_revision=config_revision), server
    )
    if endpoint.startswith("unix:"):
        Path(endpoint.removeprefix("unix:")).parent.mkdir(parents=True, exist_ok=True)
    port = server.add_insecure_port(endpoint)
    if not port:
        raise RuntimeError("cannot bind measurement endpoint")
    await server.start()
    return server, port


class DeliveryWorker:
    def __init__(self, outbox, transport, writer):
        self.outbox, self.transport, self.writer = outbox, transport, writer
        self.state, self.reason = "STARTING", "CONNECTING"
        self.backoff = RetryBackoff()
        self.next_network_attempt = 0.0
        self.last_live = 0.0
        self.fatal = False

    async def step(self, *, now=None, monotonic_now=None):
        mono = time.monotonic() if monotonic_now is None else monotonic_now
        now = time.time() if now is None else now
        if self.fatal or mono < self.next_network_attempt:
            return False
        prefer_live = mono - self.last_live >= 1
        row = self.outbox.next_ready(now=now, monotonic_now=mono, prefer_latest=prefer_live)
        if row is None:
            return False
        if prefer_live:
            self.last_live = mono
        result = await self.transport.send(row["payload"])
        if result.kind == "accepted":
            self.outbox.delivered(row["measurement_id"])
            self.backoff.success()
            self.state, self.reason = "HEALTHY", ""
        elif result.kind == "quarantine":
            self.outbox.quarantine(row["measurement_id"], result.reason)
            self.state, self.reason = "DEGRADED", result.reason
        elif result.kind == "fatal":
            # Stay alive to buffer and report FATAL; operator rotates secret + restarts.
            self.fatal = True
            self.state, self.reason = "FATAL", result.reason
        else:
            delay = min(60, max(self.backoff.failure(), result.retry_after))
            self.next_network_attempt = mono + delay
            self.outbox.retry(row["measurement_id"], next_attempt=now + delay)
            self.state, self.reason = "RETRYING", result.reason
        return True

    async def run(self):
        while True:
            await self.step()
            await asyncio.sleep(0.1)  # At most 10 total sends/s, including live priority.

    async def report(self):
        while True:
            self.outbox.maintain()
            stats = self.outbox.stats()
            state = self.state
            reasons = [self.reason] if self.reason else []
            if stats["lost_records"]:
                reasons.append("OUTBOX_DATA_LOSS")
                if state == "HEALTHY":
                    state = "DEGRADED"
            self.writer.write(state, reasons, **stats)
            await asyncio.sleep(5)


async def serve(args):
    config = load_runtime_config(args.config, args.status_dir, "measurement-uplink")
    if not args.listen.startswith("unix:"):
        raise ValueError("production local transport must use a Unix domain socket")
    writer = StatusWriter(
        args.status_dir,
        "measurement-uplink",
        config["edge_id"],
        config["config_revision"],
        site_id=config["site_id"],
        deployment_revision=config.get("deployment_revision"),
    )
    writer.write("STARTING")
    with Outbox(args.database) as outbox, Watchdog() as watchdog:
        async with httpx.AsyncClient(trust_env=False) as http:
            transport = DeliveryClient(args.url, read_token(args.token_file), client=http)
            server, _ = await make_server(
                outbox,
                args.listen,
                edge_id=config["edge_id"],
                config_revision=config["config_revision"],
            )
            worker = DeliveryWorker(outbox, transport, writer)
            try:
                await supervise(
                    termination_event(), worker.run(), worker.report(), watchdog.pulse()
                )
            finally:
                await server.stop(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--status-dir", default="/run/ajin-edge/status")
    parser.add_argument("--listen", default="unix:/run/ajin-edge/measurement.sock")
    parser.add_argument("--database", default="/var/lib/ajin-edge/outbox.db")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    asyncio.run(serve(parser.parse_args()))
