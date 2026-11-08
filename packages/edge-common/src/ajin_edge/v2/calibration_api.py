"""Authenticated Pi calibration ingress; backend ACK and service application are separate."""

import asyncio
import hmac
import ipaddress
import json
from pathlib import Path

import grpc
from aiohttp import web

from ajin_edge.lifecycle import Watchdog, supervise
from ajin_edge.transport import read_token

from .configuration_state import ControlError, ControlState
from .ipc import OPTIONS, DeliveryStub

TASK = web.AppKey("calibration_worker", asyncio.Task)
ERROR = web.AppKey("calibration_error", dict)
BODY_LIMIT = 32 * 1024


def listener(config):
    settings = config["calibration_api"]
    host, port = settings.get("host", "127.0.0.1"), settings.get("port", 8765)
    address = ipaddress.ip_address(host)
    tail = (address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")) or (
        address.version == 6 and address in ipaddress.ip_network("fd7a:115c:a1e0::/48")
    )
    if not (address.is_loopback or tail) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("control listener must use loopback or an explicit Tailscale address")
    token_file = Path(settings["token_file"])
    if not token_file.is_absolute() or token_file.resolve() == Path(config["token_file"]).resolve():
        raise ValueError("a separate absolute control credential file is required")
    if len(read_token(token_file)) < 32:
        raise ValueError("control credential must contain at least 32 ASCII characters")
    return host, port, token_file


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)


def create_app(control, delivery, token_file):
    token_file = Path(token_file)
    if len(read_token(token_file)) < 32:
        raise ValueError("control credential must contain at least 32 ASCII characters")
    active_requests = 0

    def error(code, status):
        return web.json_response({"accepted": False, "error": {"code": code}}, status=status)

    @web.middleware
    async def authenticated(request, handler):
        nonlocal active_requests
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return error("UNAUTHORIZED", 401)
        supplied = authorization[7:]
        if not supplied or any(not 33 <= ord(c) <= 126 for c in supplied):
            return error("UNAUTHORIZED", 401)
        try:
            if not hmac.compare_digest(supplied, read_token(token_file)):
                return error("UNAUTHORIZED", 401)
        except (OSError, ValueError):
            return error("CONTROL_CREDENTIAL_UNAVAILABLE", 503)
        if active_requests >= 4:
            return error("CONTROL_BUSY", 429)
        active_requests += 1
        try:
            return await handler(request)
        except ControlError as failure:
            return error(failure.code, failure.status)
        except web.HTTPRequestEntityTooLarge:
            return error("BODY_TOO_LARGE", 413)
        except web.HTTPException as failure:
            return error("HTTP_REQUEST_REJECTED", failure.status)
        except TimeoutError:
            return error("REQUEST_TIMEOUT", 408)
        except (ValueError, TypeError, KeyError):
            return error("INVALID_REQUEST", 422)
        except OSError:
            return error("CONTROL_STORAGE_UNAVAILABLE", 503)
        finally:
            active_requests -= 1

    app = web.Application(
        client_max_size=BODY_LIMIT,
        middlewares=[authenticated],
        handler_args={
            "auto_decompress": False,
            "keepalive_timeout": 10,
            "max_headers": 32,
            "max_field_size": 4096,
            "lingering_time": 0,
        },
    )
    app[ERROR] = {"code": ""}

    async def get_calibrations(request):
        return web.json_response(control.snapshot())

    async def put_calibrations(request):
        if request.content_type != "application/json":
            raise ControlError("JSON_REQUIRED", 415)
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ControlError("CONTENT_ENCODING_UNSUPPORTED", 415)
        if request.content_length is not None and request.content_length > BODY_LIMIT:
            raise ControlError("BODY_TOO_LARGE", 413)
        async with asyncio.timeout(5):
            body = await request.read()
        request_id = request.headers.get("Idempotency-Key")
        if request_id is None:
            raise ControlError("IDEMPOTENCY_KEY_REQUIRED")
        reply = control.propose(strict_json(body), request_id)
        return web.json_response(
            {"accepted": True, **reply}, status=200 if reply["state"] == "APPLIED" else 202
        )

    async def get_job(request):
        job = control.job(request.match_info["job_id"])
        if job["state"] == "PENDING_BACKEND_ACK" and app[ERROR]["code"]:
            job["reason_code"] = app[ERROR]["code"]
        return web.json_response(job)

    app.add_routes(
        [
            web.get("/api/control/v1/calibrations", get_calibrations),
            web.put("/api/control/v1/calibrations", put_calibrations),
            web.get("/api/control/v1/calibration-jobs/{job_id}", get_job),
        ]
    )

    async def drive():
        while True:
            try:
                await control.advance(delivery)
                app[ERROR]["code"] = ""
            except grpc.aio.AioRpcError:
                app[ERROR]["code"] = "LOCAL_DELIVERY_UNAVAILABLE"
            except (ControlError, OSError):
                app[ERROR]["code"] = "CONTROL_STORAGE_UNAVAILABLE"
            await asyncio.sleep(0.25)

    async def worker_context(application):
        task = asyncio.create_task(drive())
        application[TASK] = task
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(worker_context)
    return app


async def run(config, stop):
    from .runtime import LocalStatus

    host, port, token_file = listener(config)
    with ControlState(config) as control:
        async with grpc.aio.insecure_channel(
            config["delivery_endpoint"], options=OPTIONS
        ) as channel:
            app = create_app(control, DeliveryStub(channel), token_file)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
            status = LocalStatus(config, "calibration-api")
            try:
                await runner.setup()
                await web.TCPSite(runner, host, port).start()

                async def heartbeat():
                    while True:
                        if app[TASK].done():
                            await app[TASK]
                        status.config = {
                            **config,
                            "configuration": control.state["active_registration"],
                        }
                        status.reason = app[ERROR]["code"]
                        status.advanced()
                        await asyncio.to_thread(
                            status.write, "DEGRADED" if status.reason else "HEALTHY"
                        )
                        await asyncio.sleep(1)

                with Watchdog() as watchdog:
                    await supervise(stop, heartbeat(), watchdog.pulse())
            finally:
                await runner.cleanup()
