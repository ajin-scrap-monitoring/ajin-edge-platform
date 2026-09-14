import json

import grpc
import pytest
from ajin_edge.transport import DeliveryResult
from ajin_edge.wire import delivery_pb2, delivery_pb2_grpc
from ajin_measurement_uplink.outbox import Outbox
from ajin_measurement_uplink.runtime import DeliveryWorker, make_server


async def test_real_grpc_ack_is_durable_and_idempotent(tmp_path, measurement):
    path = tmp_path / "outbox.db"
    with Outbox(path) as outbox:
        server, port = await make_server(
            outbox, "127.0.0.1:0", edge_id="edge-1", config_revision="rev-1"
        )
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                sink = delivery_pb2_grpc.MeasurementSinkStub(channel)
                request = delivery_pb2.EnqueueRequest(
                    measurement_json=json.dumps(measurement).encode()
                )
                first = await sink.Enqueue(request, timeout=2)
                assert first.measurement_id == measurement["measurement_id"]
                assert first.duplicate is False
                second = await sink.Enqueue(request, timeout=2)
                assert second.duplicate is True
                measurement["config_revision"] = "other"
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    await sink.Enqueue(
                        delivery_pb2.EnqueueRequest(
                            measurement_json=json.dumps(measurement).encode()
                        ),
                        timeout=2,
                    )
                assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                assert outbox.stats()["pending"] == 1
        finally:
            await server.stop(0)
    with Outbox(path) as restored:
        assert restored.stats()["pending"] == 1


async def test_backend_lost_ack_retries_same_id_and_auth_failure_stops_network(
    tmp_path, measurement
):
    class Backend:
        def __init__(self):
            self.ids = []
            self.outcomes = [
                DeliveryResult("retry", "LOST_ACK"),
                DeliveryResult("accepted", "ACCEPTED"),
                DeliveryResult("fatal", "AUTHENTICATION_FAILED"),
            ]

        async def send(self, payload):
            self.ids.append(payload["measurement_id"])
            return self.outcomes.pop(0)

    backend = Backend()
    clock = [100.0]
    with Outbox(tmp_path / "outbox.db", monotonic_clock=lambda: clock[0]) as outbox:
        outbox.enqueue(measurement, now=100)
        worker = DeliveryWorker(outbox, backend, None)

        async def step(now):
            clock[0] = now
            return await worker.step(now=now, monotonic_now=now)

        assert await step(100)
        assert outbox.stats()["pending"] == 1
        assert not await step(100.5)
        assert await step(103)
        assert backend.ids == [measurement["measurement_id"]] * 2
        assert outbox.stats()["pending"] == 0
        measurement["measurement_id"] = "second"
        outbox.enqueue(measurement, now=104)
        assert await step(104)
        assert worker.state == "FATAL"
        assert not await step(200)
        assert outbox.stats()["pending"] == 1
