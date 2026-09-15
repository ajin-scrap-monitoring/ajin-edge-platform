"""Exercise the packaged module entrypoint without a camera or network."""

import os
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch


def main():
    class Camera:
        streams = SimpleNamespace(video=[object()])
        closed = False

        def demux(self, stream):
            return iter(())

        def close(self):
            self.closed = True

    camera = Camera()

    async def stop_at_transport(*args, **kwargs):
        raise StopAsyncIteration("transport reached")

    environment = {
        "CAMERA_ID": "test-camera",
        "CAMERA_TOKEN": "test-only",
        "MEDIA_WSS_URL": "wss://media.example/ingest",
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.dict(sys.modules, {"av": SimpleNamespace(open=lambda *a, **k: camera)}),
        patch("websockets.asyncio.client.connect", stop_at_transport),
    ):
        try:
            runpy.run_module("camera_edge.main", run_name="__main__")
        except StopAsyncIteration as error:
            if str(error) != "transport reached":
                raise
        else:
            raise RuntimeError("transport was not reached")
    if not camera.closed:
        raise RuntimeError("camera was not closed")
    print("module startup and cleanup passed")


if __name__ == "__main__":
    main()
