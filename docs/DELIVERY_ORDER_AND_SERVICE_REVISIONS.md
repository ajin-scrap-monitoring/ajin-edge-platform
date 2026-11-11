# Periodic delivery order and service status revisions

PERIODIC measurements reserve one durable Outbox record at a time, in original registration order. A newer periodic record is not selected while an older one is pinned for delivery or durable ACK persistence. A retry retains the oldest pending record. Quarantine remains an explicit terminal outcome; data is not silently discarded to advance the queue.

EVIDENCE and assessment traffic retain bounded parallel delivery. Configuration remains prioritized, one fresh STATE opportunity is reserved per selection batch, and the remaining slots alternate oldest/newest independent messages. Existing storage limits and loss accounting remain unchanged. During an outage, preserving order may delay newer periodic measurements; no freshness threshold is relaxed. This cannot guarantee remote processing order for a timed-out request that continues executing remotely.

`camera_config_revision` identifies the camera configuration independently of LiDAR calibration. If omitted, the base runtime registration revision is used before applying calibration control state. Camera identity, freshness (15 seconds), and configured revision are still validated. LiDAR calibration activation does not falsely invalidate a running camera.

Local uplink diagnostics include `http_delivery.PERIODIC.recent_attempts` (at most 16 completed attempts). Fields: message_id, generated_at, registered_at, send_started_at, completed_at, elapsed_ms, result, reason, received_at. Registration time comes from the durable record's UTC wall-clock order; send/completion timestamps are Pi UTC wall-clock observations; elapsed_ms uses a monotonic clock. The ring is volatile, rotates, and is not a historical audit log. It contains no point arrays, video, or credentials. Poll and deduplicate by message ID plus send-start time during an agreed diagnostic window. In-flight/cancelled attempts may not have a completed entry.

Accepted status diagnostics retain the exact accepted message ID, generation/receipt time and owned copies of service/sensor status fields. This proves the sender validated the ACK for that body, not dashboard rendering or database internals. The backend wire contract is unchanged.

A periodic ACK triggers immediate durable ACK flushing and reopens selection after persistence. The next periodic record can therefore proceed while a slower independent EVIDENCE request is still running, without overlapping periodic requests or bypassing durable confirmation.


The storage owner remains one thread and retains every file/directory synchronization barrier. Pending PERIODIC registration and its ACK writes have priority over queued background work; selection has the next priority. After four urgent operations, the oldest background operation gets a turn. Priority does not interrupt an already-running filesystem operation or change durability ordering. Recent operations whose queue or execution time exceeded 250 ms are exposed in a bounded 16-entry local diagnostic ring without payloads. This is measurement, not a relaxed health criterion.
