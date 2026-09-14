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

## Out of scope

- Browser protocol choice
- Recording and retention policy
- SD card mount mapping
- Actual `MEDIA_WSS_URL`
- Camera token issuance
- Credentials and deployment logic
