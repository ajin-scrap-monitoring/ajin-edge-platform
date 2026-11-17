import asyncio
import time

import pytest
from ajin_edge.v2.orchestration import Orchestrator
from ajin_edge.v2.runtime import wait_for_current_pair
from test_v2_judgment import judgment_configuration, observation


def prepared():
    cfg = judgment_configuration()
    orch = Orchestrator(cfg)
    ms = time.monotonic_ns() // 1000000
    for sid, stamp in [("lidar-a", ms), ("lidar-b", ms - 101)]:
        orch.ingest(observation(cfg, sid, 1, stamp), now_ns=time.monotonic_ns())
    return cfg, orch, ms


async def test_waits_for_new_lagging_scan_without_changing_timestamps():
    cfg, orch, ms = prepared()
    changed = asyncio.Event()

    async def arrive():
        await asyncio.sleep(0.01)
        orch.ingest(observation(cfg, "lidar-b", 2, ms), now_ns=time.monotonic_ns())
        changed.set()

    task = asyncio.create_task(arrive())
    result = await wait_for_current_pair(orch, changed)
    await task
    assert result.state == "ALIGNED"
    assert orch.judgment.history["lidar-b"][-1]["scan"]["acquired_monotonic_ns"] == str(
        ms * 1000000
    )


async def test_real_mismatch_is_bounded_and_not_hidden():
    _, orch, _ = prepared()
    before = orch.judgment.snapshot(time.monotonic_ns())
    started = time.monotonic()
    assert (await wait_for_current_pair(orch, asyncio.Event(), timeout=0.02)).state == "TIMEOUT"
    assert time.monotonic() - started < 0.5
    assert orch.judgment.snapshot(time.monotonic_ns()) == before
    assert "SENSOR_TIME_MISMATCH" in orch.judgment.calculate(time.monotonic_ns())[1]


async def test_missing_sensor_does_not_stall_and_cancellation_propagates():
    _, orch, _ = prepared()
    orch.judgment.history["lidar-b"].clear()
    assert (await wait_for_current_pair(orch, asyncio.Event())).state == "NOT_READY"
    _, orch, _ = prepared()
    task = asyncio.create_task(wait_for_current_pair(orch, asyncio.Event()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_existing_matching_pair_returns_without_waiting():
    cfg, orch, ms = prepared()
    orch.ingest(observation(cfg, "lidar-b", 2, ms), now_ns=time.monotonic_ns())
    assert (await wait_for_current_pair(orch, asyncio.Event(), timeout=0)).state == "ALIGNED"


async def test_stale_or_different_clock_is_not_treated_as_aligned():
    _, orch, _ = prepared()
    orch.judgment.history["lidar-b"][-1]["scan"]["clock_domain_id"] = "other-boot"
    assert (await wait_for_current_pair(orch, asyncio.Event())).state == "NOT_READY"


async def test_state_attempt_trace_preserves_reason_and_is_bounded(monkeypatch):
    from ajin_edge.v2.transport import DeliveryClient, Result

    client = DeliveryClient("https://backend.example", "test", None)

    async def respond(_):
        return Result("accepted", received_at="2026-11-17T00:00:00Z")

    monkeypatch.setattr(client, "_send", respond)
    for i in range(20):
        await client.send(
            {
                "message_id": str(i),
                "message_type": "edge_assessment",
                "assessment_type": "STATE",
                "generated_at": "2026-11-17T00:00:00Z",
                "reason_codes": ["SENSOR_TIME_MISMATCH"],
            }
        )
    trace = client.metrics["STATE"]["recent_attempts"]
    assert len(trace) == 16
    assert trace[-1]["judgment_reason_codes"] == ["SENSOR_TIME_MISMATCH"]
    assert trace[-1]["scan_times"] == []
