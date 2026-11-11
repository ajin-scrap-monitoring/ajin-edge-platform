"""Private UDS gRPC methods carrying canonical UTF-8 JSON (not protobuf messages)."""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import grpc

from .contracts import validate_message
from .outbox import CapacityError, ConflictError
from .processing import canonical

SERVICE = "ajin.edge.v2.Delivery"
OBSERVATIONS = "/ajin.edge.v2.Observations/Subscribe"
BUNDLE_RPC_BYTES = 16 * 1024 * 1024
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

    async def enqueue_bundle(self, messages):
        if not isinstance(messages, list) or not 2 <= len(messages) <= 17:
            raise ValueError("invalid evidence bundle size")
        assessment = messages[-1]
        manifest, sizes = [], []
        for payload in messages[:-1]:
            encoded = canonical(payload)
            manifest.append(
                {"message_id": payload["message_id"], "digest": hashlib.sha256(encoded).hexdigest()}
            )
            sizes.append(len(encoded))
        request = {"assessment": assessment}
        await self._call("Bundle", {**request, "operation": "begin", "evidence": manifest})
        try:
            batch = []
            base_size = len(canonical({**request, "operation": "stage", "messages": []}))
            batch_size = base_size
            for payload, size in zip(messages[:-1], sizes, strict=True):
                added = size + bool(batch)
                if batch and (len(batch) == 4 or batch_size + added > BUNDLE_RPC_BYTES):
                    await self._call("Bundle", {**request, "operation": "stage", "messages": batch})
                    batch = []
                    batch_size = base_size
                    added = size
                if batch_size + added > BUNDLE_RPC_BYTES:
                    raise ValueError("individual evidence exceeds internal RPC capacity")
                batch.append(payload)
                batch_size += added
            if batch:
                await self._call("Bundle", {**request, "operation": "stage", "messages": batch})
            return await self._call("Bundle", {**request, "operation": "commit"})
        except BaseException as error:
            permanent = isinstance(error, ValueError) or (
                isinstance(error, grpc.aio.AioRpcError)
                and error.code()
                in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.ALREADY_EXISTS)
            )
            if permanent:
                try:
                    await self._call("Bundle", {**request, "operation": "abort"})
                except grpc.aio.AioRpcError:
                    pass
            # A retryable failure/cancellation retains the bounded persisted
            # lease. Otherwise already ACKed evidence could be collected before
            # the same pending bundle retries. Abandoned leases still expire.
            raise

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

    async def bundle(request, context):
        try:
            assessment = request["assessment"]
            if assessment.get("site_id") != site_id or assessment.get("edge_id") != edge_id:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "device identity mismatch")
            operation = request["operation"]
            duplicate = False
            if operation == "begin":
                await store.call("begin_bundle", assessment, request["evidence"])
            elif operation == "stage":
                await store.call("stage_bundle", assessment, request["messages"])
            elif operation == "commit":
                duplicate = await store.call("commit_bundle", assessment)
            elif operation == "abort":
                await store.call("release", assessment["message_id"])
            else:
                raise ValueError("invalid bundle operation")
            return {
                "state": "pending",
                "message_id": assessment["message_id"],
                "duplicate": duplicate,
            }
        except ConflictError:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, "message ID conflict")
        except CapacityError:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "storage unavailable")
        except (ValueError, TypeError, KeyError, AttributeError):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid judgment bundle")

    async def stats(request, context):
        return await store.call("stats", audit=False)

    async def bounded_bundle(decoded, context):
        request, size = decoded
        if size > BUNDLE_RPC_BYTES:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "bundle RPC capacity exceeded")
        return await bundle(request, context)

    handlers = {
        name: grpc.unary_unary_rpc_method_handler(
            callback,
            request_deserializer=(lambda data: (json.loads(data), len(data)))
            if name == "Bundle"
            else json.loads,
            response_serializer=canonical,
        )
        for name, callback in (
            ("Enqueue", enqueue),
            ("Bundle", bounded_bundle),
            ("Inspect", inspect),
            ("Stats", stats),
        )
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
        self.instance_id = str(uuid4())

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
            queue.put_nowait(
                {
                    "observation": observation,
                    "dropped_observations": self.dropped,
                    "stream_instance_id": self.instance_id,
                }
            )

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
