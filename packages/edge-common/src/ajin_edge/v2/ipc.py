"""Private UDS gRPC methods carrying canonical UTF-8 JSON (not protobuf messages)."""

import asyncio
import json
from pathlib import Path

import grpc

from .contracts import validate_message
from .outbox import CapacityError, ConflictError
from .processing import canonical

SERVICE = "ajin.edge.v2.Delivery"
OBSERVATIONS = "/ajin.edge.v2.Observations/Subscribe"
# Internal transport allocation guard; not a backend request-size or point-selection policy.
OPTIONS = [
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),
    ("grpc.max_send_message_length", 64 * 1024 * 1024),
    ("grpc.default_authority", "localhost"),
]


class DeliveryStub:
    def __init__(self, channel):
        self.channel = channel

    async def _call(self, method, value):
        return await self.channel.unary_unary(
            f"/{SERVICE}/{method}", request_serializer=canonical, response_deserializer=json.loads
        )(value, timeout=5)

    async def enqueue(self, payload):
        return await self._call("Enqueue", payload)

    async def inspect(self, message_id):
        return await self._call("Inspect", {"message_id": message_id})

    async def stats(self):
        return await self._call("Stats", {})


async def serve_delivery(store, endpoint, *, site_id, edge_id, latest_status=None):
    async def enqueue(payload, context):
        if payload.get("site_id") != site_id or payload.get("edge_id") != edge_id:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "device identity mismatch")
        try:
            if payload["message_type"] == "edge_status":
                validate_message(payload)
                if latest_status is None:
                    raise ValueError("status sink unavailable")
                latest_status["payload"] = payload
                return {"state": "latest_snapshot", "message_id": payload["message_id"]}
            duplicate = await store.call("enqueue", payload)
            return {"state": "pending", "message_id": payload["message_id"], "duplicate": duplicate}
        except ConflictError:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "message ID conflict")
        except CapacityError:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "storage unavailable")
        except (ValueError, TypeError, KeyError):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid v2 message")

    async def inspect(request, context):
        mid = request.get("message_id")
        return await store.call("inspect", mid)

    async def stats(request, context):
        return await store.call("stats")

    handlers = {
        name: grpc.unary_unary_rpc_method_handler(
            callback, request_deserializer=json.loads, response_serializer=canonical
        )
        for name, callback in (("Enqueue", enqueue), ("Inspect", inspect), ("Stats", stats))
    }
    return await _server(endpoint, SERVICE, handlers)


async def _server(endpoint, name, handlers):
    server = grpc.aio.server(options=OPTIONS, maximum_concurrent_rpcs=16)
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(name, handlers),))
    if endpoint.startswith("unix:"):
        Path(endpoint.removeprefix("unix:")).parent.mkdir(parents=True, exist_ok=True)
    port = server.add_insecure_port(endpoint)
    if not port:
        raise RuntimeError("local endpoint unavailable")
    await server.start()
    return server, port


class ObservationBroker:
    def __init__(self, capacity=8):
        self.capacity = capacity
        self.queues = set()
        self.dropped = 0

    def subscribe(self):
        if len(self.queues) >= 8:
            raise CapacityError("observation subscriber limit")
        queue = asyncio.Queue(self.capacity)
        self.queues.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.queues.discard(queue)

    def publish(self, observation):
        if not self.queues:
            self.dropped += 1
        for queue in self.queues:
            if queue.full():
                queue.get_nowait()
                self.dropped += 1
            queue.put_nowait({"observation": observation, "dropped_observations": self.dropped})

    async def serve(self, endpoint):
        async def subscribe(request, context):
            try:
                queue = self.subscribe()
            except CapacityError:
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "subscriber limit")
            try:
                while True:
                    yield await queue.get()
            finally:
                self.unsubscribe(queue)

        return await _server(
            endpoint,
            "ajin.edge.v2.Observations",
            {
                "Subscribe": grpc.unary_stream_rpc_method_handler(
                    subscribe, request_deserializer=json.loads, response_serializer=canonical
                )
            },
        )
