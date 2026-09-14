from copy import deepcopy

import pytest
from ajin_measurement_uplink.outbox import DuplicateConflict, Outbox


def test_commit_survives_reopen_and_duplicate_does_not_enqueue_twice(tmp_path, measurement):
    path = tmp_path / "outbox.db"
    with Outbox(path) as outbox:
        assert outbox.enqueue(measurement, now=100) is False
    with Outbox(path) as outbox:
        assert outbox.stats()["pending"] == 1
        assert outbox.enqueue(measurement, now=101) is True
        row = outbox.next_ready(now=101)
        assert row["measurement_id"] == measurement["measurement_id"]
        outbox.delivered(row["measurement_id"], now=102)
    with Outbox(path) as outbox:
        assert outbox.enqueue(measurement, now=103) is True
        assert outbox.stats()["pending"] == 0


def test_conflicting_duplicate_cannot_overwrite_committed_data(tmp_path, measurement):
    with Outbox(tmp_path / "outbox.db") as outbox:
        outbox.enqueue(measurement, now=100)
        changed = deepcopy(measurement)
        changed["fill_ratio"], changed["fill_percent"] = 0.6, 60
        with pytest.raises(DuplicateConflict):
            outbox.enqueue(changed, now=101)
        assert outbox.next_ready(now=101)["payload"]["fill_percent"] == 50


def test_count_and_age_eviction_are_persisted_as_loss(tmp_path, measurement):
    path = tmp_path / "outbox.db"
    clock = [100.0]
    with Outbox(
        path, max_records=2, max_age_seconds=20, monotonic_clock=lambda: clock[0]
    ) as outbox:
        for i in range(3):
            item = deepcopy(measurement)
            item["measurement_id"] = f"id-{i}"
            outbox.enqueue(item, now=100 + i)
        assert outbox.stats()["pending"] == 2
        assert outbox.stats()["lost_records"] == 1
        clock[0] = 125
        outbox.maintain(now=125)
        assert outbox.stats()["pending"] == 0
    with Outbox(path) as outbox:
        assert outbox.stats()["lost_records"] == 3
        assert outbox.stats()["last_lost_id"] == "id-2"


def test_retry_backoff_does_not_block_new_live_record(tmp_path, measurement):
    clock = [100.0]
    with Outbox(tmp_path / "outbox.db", monotonic_clock=lambda: clock[0]) as outbox:
        outbox.enqueue(measurement, now=100)
        outbox.retry(measurement["measurement_id"], next_attempt=160)
        item = deepcopy(measurement)
        item["measurement_id"] = "live"
        outbox.enqueue(item, now=101)
        assert outbox.next_ready(now=102, prefer_latest=True)["measurement_id"] == "live"
        clock[0] = 161
        assert outbox.next_ready(now=161)["measurement_id"] == measurement["measurement_id"]


def test_poison_record_is_quarantined_and_not_retried(tmp_path, measurement):
    with Outbox(tmp_path / "outbox.db") as outbox:
        outbox.enqueue(measurement, now=100)
        outbox.quarantine(measurement["measurement_id"], "HTTP_422", now=101)
        assert outbox.next_ready(now=102) is None
        assert outbox.stats()["quarantined"] == 1
        assert outbox.stats()["lost_records"] == 1


def test_thirty_minute_simulated_outage_is_durable_and_order_is_recoverable(tmp_path, measurement):
    path = tmp_path / "outbox.db"
    with Outbox(path) as outbox:
        for second in range(1800):
            item = {**measurement, "measurement_id": f"blackout-{second}"}
            outbox.enqueue(item, now=10_000 + second)
        assert outbox.stats()["pending"] == 1800
        assert outbox.stats()["lost_records"] == 0
    with Outbox(path) as restored:
        assert restored.next_ready(now=12_000)["measurement_id"] == "blackout-0"
        assert (
            restored.next_ready(now=12_000, prefer_latest=True)["measurement_id"] == "blackout-1799"
        )
        restored.delivered("blackout-0", now=12_000)
        assert restored.next_ready(now=12_000)["measurement_id"] == "blackout-1"


def test_oversized_or_invalid_records_never_receive_durable_acceptance(tmp_path, measurement):
    with Outbox(tmp_path / "outbox.db", max_bytes=10) as outbox:
        with pytest.raises(ValueError):
            outbox.enqueue(measurement)
        assert outbox.stats()["pending"] == 0
    with Outbox(tmp_path / "invalid.db") as outbox:
        measurement["quality"]["confidence"] = float("nan")
        with pytest.raises(ValueError):
            outbox.enqueue(measurement)
        assert outbox.stats()["pending"] == 0
