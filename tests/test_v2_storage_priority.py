import asyncio
import threading

from ajin_edge.v2.outbox import Outbox
from test_v2_contracts import message


async def test_periodic_registration_passes_queued_evidence_work(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    with Outbox(tmp_path) as store:

        def block():
            entered.set()
            release.wait(3)

        monkeypatch.setattr(store, "hold", block, raising=False)
        monkeypatch.setattr(store, "background", lambda: calls.append("background"), raising=False)
        original = store.enqueue

        def enqueue(body):
            calls.append("periodic")
            return original(body)

        monkeypatch.setattr(store, "enqueue", enqueue)
        first = asyncio.create_task(store.call("hold"))
        assert await asyncio.to_thread(entered.wait, 1)
        background = [asyncio.create_task(store.call("background")) for _ in range(3)]
        await asyncio.sleep(0.01)
        periodic = asyncio.create_task(store.call("enqueue", message()))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, *background, periodic)
        assert calls[0] == "periodic"
        assert calls.count("background") == 3


def test_background_progress_is_guaranteed_between_urgent_bursts():
    from ajin_edge.v2.storage_executor import StorageExecutor

    entered, release = threading.Event(), threading.Event()
    calls = []
    with StorageExecutor() as executor:

        def hold():
            entered.set()
            release.wait(3)

        first = executor.submit(hold)
        assert entered.wait(1)
        normal = executor.submit(lambda: calls.append("background"))
        urgent = [executor.submit_priority(0, lambda: calls.append("periodic")) for _ in range(12)]
        release.set()
        for future in [first, normal, *urgent]:
            future.result(2)
    assert calls.index("background") <= 4


def test_cancelled_queued_work_does_not_run_and_shutdown_drains_running_work():
    from ajin_edge.v2.storage_executor import StorageExecutor

    entered, release = threading.Event(), threading.Event()
    calls = []
    executor = StorageExecutor()

    def hold():
        entered.set()
        release.wait(3)
        calls.append("running")

    first = executor.submit(hold)
    assert entered.wait(1)
    cancelled = executor.submit_priority(0, lambda: calls.append("cancelled"))
    assert cancelled.cancel()
    retained = executor.submit(lambda: calls.append("retained"))
    release.set()
    executor.shutdown()
    assert first.done() and retained.done()
    assert calls == ["running", "retained"]


async def test_periodic_ack_priority_keeps_crash_replay_proof(tmp_path):
    body = message()
    with Outbox(tmp_path) as store:
        await store.call("enqueue", body)
        await store.call(
            "delivered_many", [(body["message_id"], "2026-09-20T01:00:00Z")], _priority=0
        )
    with Outbox(tmp_path) as recovered:
        assert recovered.inspect(body["message_id"])["remote_acked"]
        assert recovered.stats()["lost_messages"] == 0
