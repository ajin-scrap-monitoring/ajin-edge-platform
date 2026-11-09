# camera-edge

현재 이 프로젝트는 `ajin-edge-platform/services/camera-edge`에 있다. 자체 pyproject/lock과
Docker build context는 유지한다. 저장소 전체의 [아키텍처·남은 구성 요소](../../docs/ARCHITECTURE.md)와
[배포 절차](../../docs/OPERATIONS.md)를 함께 참고한다. 영상 수신 서버는 별도
`ajin-camera-media-service` 저장소이며 브라우저 전달·녹화와는 구분한다.

Runtime packaging for the edge ingest service.

## Local checks

```bash
uv sync --frozen --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Runtime contract

- Python 3.13
- Source layout under `src/camera_edge`
- JPEG frames are sent as binary WebSocket messages
- The stream loop sends the immutable frame paired with its selected generation.
  `WssPublisher.publish_once` accepts that frame's bytes (or `None`), not the live
  capture slot. Frames replaced before selection may be skipped; a later capture
  cannot replace an already selected send or cause that generation to be sent twice.
- No decoding, re-encoding, recording, or browser delivery is defined here
- Edge defaults:
  - `/dev/video0`
  - `1920x1080`
  - `30fps`
  - `mjpeg`
  - send timeout `2`
  - ping interval and timeout `20`
  - reconnect cap `30`
  - max frame `4194304`
  - stats interval `60`
  - `LOG_FORMAT=json`

## Producer diagnostics

The entrypoint emits payload-free JSON logs: `edge_stats` every configured stats
interval and `edge_stage_delay` when a stage takes at least 0.5 seconds (at most
one warning per stage per 30 seconds). `at` is the UTC observation time, not an
original camera exposure timestamp. Tokens, transport URLs, JPEG data and raw
exception text are excluded.

`diagnostics` contains `count`, `total_seconds`, and `max_seconds` per stage for
the current stats window. Status-file reads do not reset it; each periodic stats
log closes the window. Frame and byte counters remain cumulative. Stages are:

- `capture_native`: native device read and JPEG validation/publication in the worker.
- `capture_resume`: worker completion to event-loop resumption.
- `capture_interval`: consecutive successful native capture completions.
- `send_await`: publish await duration, including transport backpressure and loop scheduling;
  it is not a network RTT or proof of delivery at the receiver.
- `send_interval`: consecutive successful send completions, including reconnect waits.
- `loop_lag`: scheduling delay relative to a 100 ms heartbeat.
- `status_write`: complete atomic status-file update including filesystem synchronization.

`selected_generation_skips` counts latest-slot generations replaced before send
selection within a connection. It excludes generations present at connection
establishment and does not measure original camera frame loss. No frame identity
or source exposure timestamp is added to the binary protocol.

Status writes run outside the streaming event loop, with one serialized writer.
Shutdown waits for an in-progress write, including after repeated cancellation;
an unresponsive filesystem can therefore delay graceful shutdown. The status
snapshot reflects observation before writing, rather than completion of disk I/O.
Simulator publication, bridge delivery and media receipt must be correlated
separately to attribute a delay outside this process.

## Out of scope

- Browser protocol choice
- Recording and retention policy
- SD card mount mapping
- Actual `MEDIA_WSS_URL`
- Camera token issuance
- Credentials and deployment logic
