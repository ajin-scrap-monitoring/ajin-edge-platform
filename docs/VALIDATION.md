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

### 정식 이미지 게시 검증 2026-09-16

- v0.1.1 태그의 GitHub Actions가 테스트·ARM64 이미지 5종 빌드·시작 검사·게시·릴리스 첨부까지 통과했다.
- GHCR 패키지 5종 모두 private이며, 릴리스의 두 첨부 파일과 실제 이미지 digest가 일치했다.
- 불변 릴리스의 초안 생성→첨부→공개 순서를 회귀 테스트로 검증했다.
  첨부 실패 시 미공개 상태를 유지한다. 플랫폼 테스트는 133개다.
- v0.1.0의 태그·이미지를 덮어쓰지 않았으며, 디바이스의 운영 컨테이너 교체는 포함하지 않았다.
- 호스트 서비스 설치, 실제 릴리스 이미지 현장 배포, 보정·단절 복구·장시간 인수는 별도다.

### 릴리스 준비 회귀 검증 2026-09-15

- 이전 카메라 immutable-frame 전송 수정은 유지하고 모듈 시작 순서, SDK 정리,
  timesyncd exporter 수정과 회귀 테스트를 복원했다.
- Windows와 Pi ARM64 Docker에서 플랫폼 131개, 카메라 51개 테스트를 각각 독립 실행해 통과했다.
- Pi에서 서비스 이미지 5종을 새로 빌드하고 ARM64, UID 10001, 기본 실행 경로를 확인했다.
  카메라는 실제 모듈 시작/종료를 실행하되 캡처와 전송을 대체한 smoke 검사다.
- SDK Release CTest 2/2 및 별도 AddressSanitizer·누수 검사 CTest 2/2를 통과했다.
- Python Ruff, 카메라 format, Git diff whitespace와 actionlint 1.7.12 검사를 통과했다.
- 기존 실행 컨테이너는 변경하지 않았다. 이번 회귀 시험은 실제 센서·USB 촬영의 재인수가 아니다.
- GHCR 첫 private 게시, 운영 systemd 설치, 현장 보정 및 장시간 인수는 아직 미검증이다.
  릴리스 워크플로의 조건은 [릴리스 절차](RELEASE.md)를 따른다.

### SDK 큐 정리 수정 검증 2026-09-14

- 고정 SDK의 잔여 Buffer 해제 회귀 테스트: 수정 전 AddressSanitizer 오류 재현,
  수정 후 빈 큐·단일/다중 항목·반복 disconnect 통과. 누수 검사 포함 CTest 2/2 통과.
- Raspberry Pi ARM64 Release Docker 빌드 및 CTest 2/2 통과. 플랫폼 Python 103개 통과.
- S2E 두 대의 동시 수집 및 정상 종료·재시작 3회 통과. 모든 종료 코드 0.
- 추가 60초 동시 수집에서 각각 574/573프레임, 관측 시점마다 HEALTHY와 증가하는 sequence 확인.
- 시작 시 grab-scan 오류 1회가 센서별로 발생했지만 크래시 없이 복구했고,
  60초 동안 오류 카운터가 추가 증가하지 않았다. 시작 시 오류 자체를 없앤 수정은 아니다.
- 이 시험은 수집·복구·종료 경로에 한정된다. gRPC 소비자 수신, 적재율 계산,
  서버 전달, 물리적 단선/재연결 및 장시간 운영 인수는 별도다.

### 플랫폼 Docker 재검증 2026-09-14

- 카메라 `python -m camera_edge.main`의 helper 정의 이전 실행 오류를 회귀 테스트로 재현 후 수정.
  import-first 우회 없이 기본 Docker CMD로 실제 USB MJPEG 전송·재시작 통과.
- timesyncd exporter CLI 회귀 테스트: 실제 NTP offset 계산, 음수/큰 offset, 동기 해제,
  stale/future 표본, spike, leap, stratum, malformed 응답, 유틸리티 부재, poll 오류 검사.
- 독립 실행 기준 ARM64 Docker 플랫폼 115개, 카메라 49개 통과. Windows에서도 각각 통과,
  양쪽 Ruff와 diff whitespace 검사 통과. 두 독립 테스트 환경을 한 pytest 프로세스에 모은
  추가 실행은 중간에 진행이 멈춰 중지했다. 통합 수집 실행의 정지 원인은 미확정이며,
  이 결과를 전체 164개 단일 실행 성공으로 표시하지 않는다.
- Pi ARM64에서 실제 S2E 2대·USB 카메라·processing·uplink·orchestrator와 격리 TLS/WSS
  수신기를 함께 실행했다. 측정 67건, heartbeat 19건, JPEG 1,493프레임 수신, 계약 오류 0건.
- 호스트 NTP 교환의 측정 offset으로 SYNCED 전달 및 camera_reference 생성 확인.
  현재 Pi에는 UID 10001의 호스트 계정이 없어 D-Bus 읽기가 거부됐다. 이번 시험은 호스트 읽기
  프로세스와 시험 파일 소유권 전달만 연결했다. 비루트 systemd 서비스의 정식 설치 검증은 남아 있다.
- orchestrator 정지 중 측정/영상 지속, camera 정지 시 상태 저하 및 재시작 복구,
  driver A 정지 시 INVALID/fill 제외 및 복구 통과.
- 시험 수신기의 HTTP 503에서 Outbox 10건 이상 누적, uplink 재시작 보존,
  수신 복구 후 pending 0 확인. 실제 운영 서버 시험이나 scan 단위 ACK 검증은 아니다.
- 시험 컨테이너는 종료했고 기존 운영 생성기는 변경하지 않았다. 원시 스캔/영상은 저장하지 않았다.
- 위 시험 당시에는 생성기 직접 연동을 실행하지 않았다. 이후 Rust UDS 생성기와의
  authority 수정 검증은 별도 수행했으며, 과거 TCP/MsgPack 생성기 전제와 구분한다.

단위 테스트와 단기 실장비 통신 시험은 현장 인수 완료가 아니다. 이번 시험은 demo 보정이며,
현장 보정 정확도, 물리적 단선/재연결, 8시간 부하, 실제 서버 단절·복구와 멱등 ACK는 별도 검증이 필요하다.
미디어 녹화·시간창 조회는 현재 구현 범위가 아니다.
완료 조건은 [아키텍처](ARCHITECTURE.md)와 [운영 절차](OPERATIONS.md)를 따른다.
