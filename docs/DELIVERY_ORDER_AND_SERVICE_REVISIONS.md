# Periodic delivery order and service status revisions

PERIODIC measurements reserve one durable Outbox record at a time, in original registration order. A newer periodic record is not selected while an older one is pinned for delivery or durable ACK persistence. A retry retains the oldest pending record. Quarantine remains an explicit terminal outcome; data is not silently discarded to advance the queue.

EVIDENCE and assessment traffic retain bounded parallel delivery. Configuration remains prioritized, one fresh STATE opportunity is reserved per selection batch, and the remaining slots alternate oldest/newest independent messages. Existing storage limits and loss accounting remain unchanged. During an outage, preserving order may delay newer periodic measurements; no freshness threshold is relaxed. This cannot guarantee remote processing order for a timed-out request that continues executing remotely.

`camera_config_revision` identifies the camera configuration independently of LiDAR calibration. If omitted, the base runtime registration revision is used before applying calibration control state. Camera identity, freshness (15 seconds), and configured revision are still validated. LiDAR calibration activation does not falsely invalidate a running camera.

When replacing the base registration independently of the camera, explicitly retain
the actual camera revision in `camera_config_revision`. A base revision change is
not a camera deployment. Validate the camera identity and current report before
setting this value. A dedicated orchestrator runtime mount can apply this setting
without restarting unrelated roles watching the shared runtime file; keep its
non-camera fields synchronized with the shared base during later deployments.

Before independent-section judgment, the orchestrator permits at most 300 ms for
a lagging new observation to arrive when the latest A/B scans exceed the existing
pair tolerance. It does not select an older matching pair or rewrite timestamps.
Missing, stale or different-clock inputs are not declared aligned. On timeout the
normal judgment runs and retains its mismatch/unavailable reasons. The wait counts
within the existing one-second judgment scheduling interval. The waiting budget covers a 100 ms scan period plus measured observation delivery latency (up to 165 ms in the diagnostic sample); it is distinct from the unchanged 100 ms pair tolerance.

The readiness check now hands its captured histories directly to the calculation
worker. Profiles, point clouds and evidence use that same input snapshot; a new
arrival cannot replace just one anchor between the check and calculation. Capture
does not extend validity: histories are filtered again against the calculation
start time, and existing completion-time expiry checks remain active. No search
for an older matching pair is introduced.

The local orchestrator status keeps at most 16 `recent_judgments`, correlating the
message ID with selection state, capture time, actual selected scan IDs/timestamps
and final reason codes. It contains no point arrays or credentials. A timeout or
genuine mismatched input remains reportable and can be distinguished from a
check-to-calculation race without modifying the backend wire contract.

Local uplink diagnostics include `http_delivery.PERIODIC.recent_attempts` (at most 16 completed attempts). Fields: message_id, generated_at, registered_at, send_started_at, completed_at, elapsed_ms, result, reason, received_at. Registration time comes from the durable record's UTC wall-clock order; send/completion timestamps are Pi UTC wall-clock observations; elapsed_ms uses a monotonic clock. The ring is volatile, rotates, and is not a historical audit log. It contains no point arrays, video, or credentials. Poll and deduplicate by message ID plus send-start time during an agreed diagnostic window. In-flight/cancelled attempts may not have a completed entry.

Accepted status diagnostics retain the exact accepted message ID, generation/receipt time and owned copies of service/sensor status fields. This proves the sender validated the ACK for that body, not dashboard rendering or database internals. The backend wire contract is unchanged.

STATE also retains a bounded 16-attempt ring. Both rings include
`judgment_reason_codes`; PERIODIC includes `scan_times` with only sensor ID, scan ID
and acquisition time. These fields permit acquisition-to-ACK correlation without
copying point arrays. They do not reconstruct attempts lost before diagnostics
were enabled, or certify the backend clock is synchronized.

A periodic ACK triggers immediate durable ACK flushing and reopens selection after persistence. The next periodic record can therefore proceed while a slower independent EVIDENCE request is still running, without overlapping periodic requests or bypassing durable confirmation.


The storage owner remains one thread and retains every file/directory synchronization barrier. Pending PERIODIC registration and its ACK writes have priority over queued background work; selection has the next priority. After four urgent operations, the oldest background operation gets a turn. Priority does not interrupt an already-running filesystem operation or change durability ordering. Recent operations whose queue or execution time exceeded 250 ms are exposed in a bounded 16-entry local diagnostic ring without payloads. This is measurement, not a relaxed health criterion.
