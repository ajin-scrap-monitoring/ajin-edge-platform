# Ajin Edge Platform

라즈베리 파이에서 실행하는 **라이다·카메라·전달·오케스트레이션** 저장소다.
ROS 없이 공식 SLAMTEC SDK와 Python을 사용한다. 측정 백엔드·DB·이벤트·대시보드는 다른 담당자의
범위이며 이 저장소에 구현하지 않는다. 같은 담당자의 서버 영상 수신기는 별도
`ajin-camera-media-service` 저장소다.

**초기 소프트웨어 구현이며 현장 배포 승인본은 아니다.** 실제 Pi/센서/카메라 통합과 승인된
보정값, 운영 서버 계약은 별도 확인해야 한다.

## 문서

- [전체 아키텍처와 남은 구성 요소](docs/ARCHITECTURE.md)
- [배포·장애 복구·현장 인수](docs/OPERATIONS.md)
- [백엔드·영상 연동 계약](docs/BACKEND_CONTRACT.md)
- [검증 기록](docs/VALIDATION.md)
- [설계 결정과 한계](docs/IMPLEMENTATION_DECISIONS.md)

## 실제 디렉터리

```text
services/
  lidar-driver/             C++ SDK 소스·테스트·CMake·Dockerfile
  lidar-processing/         src/ajin_lidar_processing + Dockerfile
  measurement-uplink/       src/ajin_measurement_uplink + Dockerfile
  camera-edge/              독립 Python 프로젝트·테스트·Dockerfile
  edge-orchestrator/        src/ajin_edge_orchestrator + Dockerfile
packages/edge-common/       src/ajin_edge: 공통 계약·상태·설정·시간·HTTPS
contracts/                 Proto / 백엔드 JSON Schema 원본
deploy/                    Pi Compose / 환경·보정 예제 / clock systemd
tests/                     라이다·전달·오케스트레이터·공통 회귀
integration-tests/         실제 로컬 gRPC / 합성 리플레이
tools/                     계약 생성 / 시각 exporter / runtime 준비
docs/                      설계·운영·검증·남은 작업
```

Python 서비스 세 개와 공통 코드는 루트 `pyproject.toml`/`uv.lock`으로 함께 패키징한다.
각 서비스의 실제 코드는 자기 폴더에 있지만 **각각 독립 프로세스·이미지로 실행**한다.
카메라는 PyAV 의존성을 격리하기 위해 `services/camera-edge`의 독립 pyproject/lock을 유지한다.

## 빠른 시작

아래는 저장소 루트 기준이며 Python 3.13과 uv가 필요하다.

```shell
uv sync --frozen --python 3.13
uv run --frozen pytest -q
uv run --frozen edge-replay --config deploy/config.example/processing.example.json --input integration-tests/replay-data/half-full-then-loss.jsonl --allow-demo
```

카메라 자체 환경은 `cd services/camera-edge` 후 `uv sync --frozen --extra dev`로 준비한다.
카메라 테스트는 하드웨어 캡처 성공을 뜻하지 않는다.

리플레이는 네트워크에 전송하지 않는 합성 테스트다. 약 50% 결과 뒤 두 센서 단절 시
`INVALID`와 적재율 필드 생략을 확인한다. `--allow-demo` 없이는 예제 보정을 거부한다.
데모 보정을 운영값으로 바꾸거나 리플레이 데이터를 운영 Uplink에 넣으면 안 된다.

## 빌드·배포 경계

라이다/전달/오케스트레이터 Docker 빌드 context는 이 저장소 루트이고, 카메라는 자기 서비스
폴더가 context다. 정확한 명령과 secret·권한 준비는 [OPERATIONS](docs/OPERATIONS.md)에 있다.
Compose는 이미지 조합을 실행할 뿐 현장 설정이나 인증정보를 생성하지 않는다.

## 이용 조건

이 Repository는 코드 검토와 참고를 위해 Public으로 제공하며 프로젝트 소스 코드에 별도 라이선스를 부여하지 않는다.
외부 라이브러리에는 각 라이선스가 적용되며 SLAMTEC SDK 고지는 `services/lidar-driver/NOTICE`에 있다.
