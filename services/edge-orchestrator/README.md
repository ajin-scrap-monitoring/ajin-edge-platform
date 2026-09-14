# Edge orchestrator

실제 Python 코드는 `src/ajin_edge_orchestrator/`에 있다. 각 서비스의 상태 JSON과 호스트
시각 상태를 읽어 ID/설정/배포 일치 여부를 확인하고 종합 Heartbeat를 보낸다.
센서·영상을 중계하거나 Docker를 직접 시작/중지하지 않는다.

설치와 Docker build context는 저장소 루트다:

```shell
uv run --frozen edge-orchestrator --help
uv run --frozen pytest tests/test_orchestrator.py tests/test_final_fixes.py
```

운영 Heartbeat 수신 계약·경보 연계, 시각/영상 상관관계, 현장 장애 시험이 남아 있다.
[전체 흐름과 남은 요소](../../docs/ARCHITECTURE.md) · [Heartbeat 계약](../../docs/BACKEND_CONTRACT.md)
