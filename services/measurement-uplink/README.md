# Measurement uplink

실제 Python 코드는 `src/ajin_measurement_uplink/`에 있다. 전처리 측정의 gRPC 수신,
SQLite commit 후 ACK, 영속 큐, HTTPS 전송/재시도·격리를 담당한다.
공통 HTTPS 구현은 `../../packages/edge-common/src/ajin_edge/transport.py`다.

설치와 Docker build context는 저장소 루트다:

```shell
uv run --frozen measurement-uplink --help
uv run --frozen pytest tests/test_outbox.py tests/test_delivery.py integration-tests/tests
```

실제 백엔드 ACK/인증 합의, 장시간 단절·용량·복구 시험이 남아 있다. 백엔드 DB나 이벤트
판정은 이 서비스가 구현하지 않는다. [서버 계약](../../docs/BACKEND_CONTRACT.md)과
[아키텍처의 남은 작업](../../docs/ARCHITECTURE.md)을 참고한다.
