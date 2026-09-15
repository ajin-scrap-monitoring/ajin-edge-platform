"""Independent subscribers and bounded asynchronous durable-sink delivery."""

import argparse
import asyncio
import json
import time
from collections import deque

import grpc
from ajin_edge.clock import read_clock
from ajin_edge.config import load_runtime_config
from ajin_edge.lifecycle import Watchdog, termination_event
from ajin_edge.status import StatusWriter
from ajin_edge.wire import delivery_pb2, delivery_pb2_grpc, lidar_pb2, lidar_pb2_grpc

from .engine import ProcessingEngine

_SENSOR_CHANNEL_OPTIONS = (
    ("grpc.max_receive_message_length", 4 * 1024 * 1024),
    ("grpc.default_authority", "localhost"),
)


def _open_sensor_channel(endpoint):
    return grpc.aio.insecure_channel(endpoint, options=_SENSOR_CHANNEL_OPTIONS)


class PendingQueue:
    def __init__(self, capacity=10):
        if capacity < 1:
            raise ValueError("capacity")
        self.items = deque(maxlen=capacity)
        self.loss = 0

    def put(self, item):
        if len(self.items) == self.items.maxlen:
            self.loss += 1
        self.items.append(item)

    def peek(self):
        return self.items[0] if self.items else None

    def ack(self, identity):
        if self.items and self.items[0]["measurement_id"] == identity:
            self.items.popleft()


async def run(args):
    config = load_runtime_config(args.config, args.status_dir, "lidar-processing")
    status = StatusWriter(
        args.status_dir,
        "lidar-processing",
        config["edge_id"],
        config["config_revision"],
        site_id=config["site_id"],
        deployment_revision=config.get("deployment_revision"),
    )
    try:
        engine = ProcessingEngine(config)
    except (ValueError, KeyError, TypeError, OverflowError):
        status.write("FATAL", reason_codes=["CALIBRATION_INVALID"])
        raise
    pending = PendingQueue()
    stop = termination_event()
    last_measurement = None
    last_quality = "INVALID"
    last_reasons = []
    watchdog = Watchdog(timeout=15)
    watchdog.__enter__()

    async def subscribe(sensor):
        delay = 1
        while not stop.is_set():
            started = time.monotonic()
            try:
                async with _open_sensor_channel(sensor["endpoint"]) as channel:
                    stream = lidar_pb2_grpc.LidarScanSourceStub(channel).SubscribeScans(
                        lidar_pb2.SubscribeRequest(consumer_id=status.instance_id)
                    )
                    async for frame in stream:
                        if frame.sensor_id == sensor["sensor_id"]:
                            engine.ingest(
                                frame,
                                receive_monotonic_ns=time.monotonic_ns(),
                                receive_unix_ms=time.time_ns() // 1_000_000,
                            )
                        else:
                            engine.frame_loss += 1
                        if time.monotonic() - started >= 30:
                            delay = 1
            except grpc.RpcError:
                pass
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass
            delay = min(30, delay * 2)

    async def deliver():
        async with grpc.aio.insecure_channel(args.uplink) as channel:
            stub = delivery_pb2_grpc.MeasurementSinkStub(channel)
            while not stop.is_set():
                item = pending.peek()
                if item:
                    try:
                        response = await stub.Enqueue(
                            delivery_pb2.EnqueueRequest(
                                measurement_json=json.dumps(item, allow_nan=False).encode()
                            ),
                            timeout=2,
                        )
                        if response.measurement_id == item["measurement_id"]:
                            pending.ack(item["measurement_id"])
                    except grpc.RpcError:
                        await asyncio.sleep(1)
                else:
                    await asyncio.sleep(0.1)

    tasks = [asyncio.create_task(subscribe(s)) for s in config["sensors"]] + [
        asyncio.create_task(deliver()),
        asyncio.create_task(watchdog.pulse()),
    ]
    try:
        while not stop.is_set():
            m = engine.measure(
                time.monotonic_ns(), time.time_ns() // 1_000_000, read_clock(args.clock_file)
            )
            if m:
                pending.put(m)
                last_measurement = m["measurement_id"]
                last_quality = m["quality"]["state"]
                last_reasons = m["quality"]["reason_codes"]
            state = (
                "RETRYING"
                if engine.blocked or last_quality == "INVALID"
                else "DEGRADED"
                if last_quality == "DEGRADED"
                else "HEALTHY"
            )
            status.write(
                state,
                reason_codes=sorted(
                    set(
                        last_reasons
                        + (
                            ["CONFIG_REVISION_MISMATCH"]
                            if engine.blocked
                            else ["MEASUREMENT_UNAVAILABLE"]
                            if last_quality == "INVALID"
                            else []
                        )
                    )
                ),
                frame_loss=engine.frame_loss,
                history_evictions=engine.history_evictions,
                local_loss_count=pending.loss,
                pending_count=len(pending.items),
                last_measurement_id=last_measurement,
            )
            for task in tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("processing task exited unexpectedly")
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        watchdog.__exit__()
        status.write("UNKNOWN", reason_codes=["STOPPED"])


def main():
    p = argparse.ArgumentParser()
    for flag in ("config", "status-dir", "clock-file", "uplink"):
        p.add_argument("--" + flag, required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
