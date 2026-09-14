# 검증 범위

## 재현 명령

저장소 루트에서 Python 3.13과 uv를 사용한다.

```shell
uv sync --frozen --python 3.13
uv run --frozen pytest -q -p no:cacheprovider
uv run --frozen ruff check packages services/lidar-processing/src services/measurement-uplink/src services/edge-orchestrator/src tests integration-tests tools conftest.py
uv build
```

카메라는 `services/camera-edge`에서 `uv sync --frozen --extra dev` 후 `uv run pytest`로 검사한다.
플랫폼 CI는 Python 검사·패키징·C++ core 테스트·Compose 구성 검사를 실행한다.
카메라 CI는 독립 환경 검사와 ARM64 컨테이너 빌드를 실행한다.

## 확인된 범위

- 계약, 좌표 변환, 보정, 센서 단절, 시간, 상태 집계, 합성 리플레이.
- SQLite durable commit 후 gRPC ACK, 재시작 복구, 중복 및 설정 revision 검증.
- 로컬 TLS 서버 통신, 인증서 검증, 재시도와 인증 중단.
- Windows Python 테스트와 Linux AMD64 SDK 드라이버 빌드 및 C++ 테스트.

## 현장 인수와 구분

단위 테스트와 가상 시간 시험은 실장비 인수가 아니다. 실제 S2E 두 대와 USB 카메라,
Pi ARM64 구동, 현장 보정 정확도, 8시간 부하, 실제 서버 단절·복구와 멱등 ACK는 별도 검증이 필요하다.
미디어 녹화·시간창 조회는 현재 구현 범위가 아니다.
완료 조건은 [아키텍처](ARCHITECTURE.md)와 [운영 절차](OPERATIONS.md)를 따른다.
