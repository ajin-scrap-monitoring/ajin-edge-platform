"""Liveness supervisor; an event-loop stall exits for bounded container restart."""

import asyncio
import os
import signal
import threading
import time


class Watchdog:
    def __init__(self, timeout=15, *, on_stall=None):
        self.timeout = timeout
        self.last_tick = time.monotonic()
        self.on_stall = on_stall or (lambda: os._exit(70))
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._monitor, name="liveness", daemon=True)

    def _monitor(self):
        while not self.stopped.wait(2):
            if time.monotonic() - self.last_tick > self.timeout:
                self.on_stall()
                return

    async def pulse(self):
        while True:
            self.last_tick = time.monotonic()
            await asyncio.sleep(2)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=3)


def termination_event():
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, event.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(event.set))
    return event


async def supervise(stop, *coroutines):
    """Unexpected worker termination stops the process, never leaves a half-alive service."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper:
                await task
                raise RuntimeError("service worker stopped unexpectedly")
    finally:
        for task in [*tasks, stopper]:
            task.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
