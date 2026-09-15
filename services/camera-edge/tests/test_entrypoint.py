"""The Docker module entrypoint must define helpers before starting capture."""

import runpy
import sys
from types import SimpleNamespace

import pytest


def test_module_entrypoint_reaches_transport_and_closes_camera(monkeypatch):
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

    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=lambda *a, **k: camera))
    monkeypatch.setattr("websockets.asyncio.client.connect", stop_at_transport)
    for key in ("CAMERA_TOKEN_FILE", "EDGE_STATUS_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CAMERA_ID", "test-camera")
    monkeypatch.setenv("CAMERA_TOKEN", "test-only")
    monkeypatch.setenv("MEDIA_WSS_URL", "wss://media.example/ingest")
    monkeypatch.delitem(sys.modules, "camera_edge.main", raising=False)
    with pytest.raises(StopAsyncIteration, match="transport reached"):
        runpy.run_module("camera_edge.main", run_name="__main__")
    assert camera.closed
