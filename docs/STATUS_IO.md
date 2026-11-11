# Nonblocking v2 status persistence

The processing, uplink and orchestrator loops previously called the synchronous
atomic status writer directly. A slow filesystem sync could stall scan receipt,
observation publication, delivery RPC handling and status evaluation on that loop.

`LocalStatus.write_async` captures an owned snapshot on the service event loop,
then performs directory accounting and the existing atomic JSON write in a worker
thread. Callers await completion: there is no unbounded background status queue.
The `fsync`, atomic replacement, storage reserve, timestamps and failure handling
remain in place. Cancellation drains the outstanding write before the service
generation exits, preventing a retired writer from overwriting a new generation.

The synchronous writer remains for synchronous callers. Async v2 runtime paths
use the async writer, including registration and failure status reporting.

This does not relax scan freshness, pairing, recovery duration or alarm thresholds.
It does not replace acquisition times with arrival times, discard queued scans, or
change raw point arrays, calibration, judgment or backend contracts.

Regression tests block persistence while verifying event-loop progress, snapshot
ownership and cancellation ordering. Live verification should compare driver and
processing output ages on the same device, monitor losses and recovery resets, and
retain the original thresholds throughout.
