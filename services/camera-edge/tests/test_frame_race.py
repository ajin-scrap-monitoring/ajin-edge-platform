import asyncio

import pytest

from camera_edge.ingest import LatestFrameSlot, Settings, WssPublisher
from camera_edge.main import run_edge_stream

FIRST = b"\xff\xd8first\xff\xd9"
SECOND = b"\xff\xd8second-longer\xff\xd9"


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite_after_selection", [True, False])
async def test_stream_keeps_selected_generation_and_frame_together(overwrite_after_selection):
    class OverwriteAfterSelection(LatestFrameSlot):
        def wait_for_newer(self, generation, timeout=None):
            if generation == 0:
                self.publish(FIRST)
                selected = (
                    super().wait_for_newer(generation, timeout)
                    if overwrite_after_selection
                    else None
                )
                # Model replacement either before or after the atomic selection.
                self.publish(SECOND)
                return selected or super().wait_for_newer(generation, timeout)
            return super().wait_for_newer(generation, timeout)

    class EmptyCapture:
        def publish_next(self, slot):
            return False

    class Socket:
        def __init__(self):
            self.frames = []

        async def send(self, frame):
            self.frames.append(frame)
            if len(self.frames) == (2 if overwrite_after_selection else 1):
                raise StopAsyncIteration

        async def close(self):
            pass

    socket = Socket()

    async def connect(*args, **kwargs):
        return socket

    settings = Settings.from_env(
        {
            "CAMERA_ID": "test-camera",
            "MEDIA_WSS_URL": "wss://media.example/ws",
            "CAMERA_TOKEN": "test-only",
        }
    )
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(
            run_edge_stream(
                settings,
                slot=OverwriteAfterSelection(),
                capture=EmptyCapture(),
                publisher=WssPublisher(),
                connect=connect,
            ),
            timeout=3,
        )
    assert socket.frames == ([FIRST, SECOND] if overwrite_after_selection else [SECOND])
