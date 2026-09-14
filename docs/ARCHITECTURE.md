# 엣지 플랫폼 아키텍처와 남은 구성 요소

기준일: 2026-09-14. 실제 소스를 기준으로 **구현**, **제안/미구현**, **현장 미검증**을 구분한다.
폴더 이동은 기능 추가가 아니며 Binary JPEG/WSS, protobuf, JSON 필드와 실행 서비스 ID는 유지했다.

## 1. 담당 및 배포 경계

담당 범위는 LiDAR 엣지, 카메라 엣지, 서버 카메라 서비스, Pi 오케스트레이션이다.
이 저장소는 그중 **Pi에 올라가는 부분**을 담는다. 서버 영상 수신은 별도
`ajin-camera-media-service`가 담당하며, 측정 백엔드·DB·확정 이벤트·알림·대시보드는 다른 담당자가
구현한다. 해당 기능을 이 저장소의 남은 구현으로 잘못 포함하지 않는다.

실행은 5종 서비스/6개 컨테이너다. LiDAR 드라이버 하나를 A/B 두 프로세스로 실행한다.
장치 재기동·컨테이너 시작/정지는 Docker Compose와 호스트 운영 절차의 역할이다.
`edge-orchestrator`는 상태 집계기이지 Docker 제어기, 데이터 중계기 또는 작업 스케줄러가 아니다.

## 2. 데이터와 상태 흐름

```text
S2E A → driver A ─┐
                  ├─ Unix socket gRPC ScanFrame → processing
S2E B → driver B ─┘                                │
                                  gRPC Enqueue(JSON bytes)
                                                   ↓
                                            measurement-uplink
                                                   ↓
                                            SQLite FULL + WAL
                                                   ↓ HTTPS + ACK
                                            다른 담당자의 백엔드

USB MJPEG → camera-edge → Binary JPEG / WSS → 별도 camera-media-service

각 서비스의 원자적 상태 JSON ─┐
호스트 chrony → clock.json ───┴→ edge-orchestrator → HTTPS Heartbeat → 백엔드
```

측정과 영상은 오케스트레이터의 생존에 의존하지 않는다. 측정의 `camera_reference`는
카메라 ID와 UTC 검색 시간창이며, 영상 자체나 확정 이벤트 ID가 아니다. 현재 영상 서버는
과거 시간창 검색을 구현하지 않았으므로 이 필드만으로 녹화 클립을 가져올 수는 없다.

## 3. 구현 모듈

| 서비스/모듈 | 실제 코드 위치 | 현재 역할 | 남은 핵심 확인 |
| --- | --- | --- | --- |
| lidar-driver A/B | `services/lidar-driver/src/main.cpp` | 고정 SDK SHA, S2E UDP, HQ 스캔, 최신 2개 버퍼, gRPC 스트림, 재연결·watchdog | 실제 2대 동시 수집·ARM64·네트워크/전원 장애 |
| lidar-processing | `services/lidar-processing/src/ajin_lidar_processing/` | 필터·좌표변환·ROI·50mm 단면·품질·두 센서 융합·보정·후보 급변/가림 | 현장 보정·단일 센서 표·정확도·Pi 1Hz 성능 |
| measurement-uplink | `services/measurement-uplink/src/ajin_measurement_uplink/` | gRPC 수신, commit 후 ACK, SQLite Outbox, HTTPS 재시도·격리·손실 통계 | 실제 서버 멱등 ACK·인증·용량/장시간 단절 |
| camera-edge | `services/camera-edge/src/camera_edge/` | USB MJPEG 캡처, JPEG 그대로 WSS 송신, backoff, 선택적 상태 파일 | native 캡처 stall·USB hotplug·실제 촬영 지연 |
| edge-orchestrator | `services/edge-orchestrator/src/ajin_edge_orchestrator/` | 서비스/릴리스/카메라 식별 확인, 종합 상태, 최신 Heartbeat | 실제 운영 감시·알림 수신자·시간창 상관 검증 |
| 공통 Python | `packages/edge-common/src/ajin_edge/` | 설정/체크섬, 상태 파일, 시각 분류, HTTPS, JSON 계약, 생성된 protobuf | 버전 일치·릴리스별 계약 회귀 유지 |
| 호스트 도구 | `tools/export_clock.py`, `tools/prepare_runtime.py` | chrony 읽기, 전용 runtime 권한/디렉터리 준비 | 호스트 설치·장치 권한·systemd 인수 |

`ajin_edge`는 공통 import 이름이며 서비스 패키지는 각각 `ajin_lidar_processing`,
`ajin_measurement_uplink`, `ajin_edge_orchestrator`다. CLI 이름은 `lidar-processing`,
`measurement-uplink`, `edge-orchestrator`, `edge-replay`, `edge-healthcheck`로 유지한다.
루트 wheel에는 공통+세 서비스 패키지가 포함된다. 소스/실행 경계와 패키지 릴리스 경계는 다르다.
카메라는 별도 wheel/lock을 사용한다. 이 구조에서 공통 패키지를 독립 배포하는 기능은 추가하지 않았다.

## 4. 통신 계약과 상태

- 드라이버→전처리: `contracts/lidar/v1/lidar.proto`, Unix socket gRPC. 원시 스캔은 서버로 보내지 않는다.
- 전처리→Uplink: `delivery.proto`의 JSON bytes. 같은 측정 ID로 재시도하고 durable commit 뒤 ACK한다.
- Uplink→백엔드: `contracts/backend/v1/measurement.schema.json`; HTTPS/Bearer/Idempotency-Key.
  서버가 동일 ID의 저장을 확인해야 Outbox에서 제거한다. 클라이언트 단독 exactly-once는 보장하지 않는다.
- 서비스→오케스트레이터: 서비스별 상태 JSON을 공유 볼륨에서 읽기. 센서 상태와 프로세스 생존을 구분한다.
- 오케스트레이터→백엔드: `edge-heartbeat.schema.json`; 약 10초 또는 의미 있는 변경 시 전송, 과거 상태 큐 없음.
- 카메라→영상 서버: Bearer로 인증한 `/api/v1/cameras/{camera_id}/stream`, 한 binary message에 한 JPEG.
  현재 Compose URL은 현장 입력값이며, 샘플의 주소를 그대로 사용하지 않는다.

일반 stale 상태는 UNKNOWN이다. 종료된 프로세스의 유효한 FATAL CONFIG 진단은
`diagnostic_stale=true`로 유지할 수 있지만 생존 신호는 아니다. 설정/배포/카메라 매핑 오류는
CONFIG_ERROR로 집계한다. 전체 판정과 서버 응답 형식은 [BACKEND_CONTRACT](BACKEND_CONTRACT.md)에 있다.

## 5. 장애·저장·시각 설계

- 스캔 2초 stale이면 이전 적재율을 새 정상값처럼 재사용하지 않는다. INVALID에는 적재율을 넣지 않는다.
- 한 센서로 계산하려면 별도 승인된 단일 센서 보정표가 필요하다. 없으면 INVALID다.
- Outbox는 건수·용량·보존 상한을 가진다. 장시간 단절 시 폐기가 가능하며 손실 ID/수신 UTC 범위를 기록한다.
- 재시도는 monotonic 시간이고, 24시간 보존 나이는 실행 중 누적 시간이다. 정지 시간은 제외되므로
  달력 기준 최대 24시간 보존 보장이 아니다. 상세 한계는 [구현 결정](IMPLEMENTATION_DECISIONS.md)에 있다.
- 호스트 chrony 상태가 미확인이면 UNSYNCED. 시각 오차가 작을 때만 영상 검색 시간창을 첨부한다.
- Compose healthcheck는 자동 재시작 트리거가 아니다. 프로세스 내부 watchdog과 운영 경보가 별개로 필요하다.
- HTTPS 인증 실패는 FATAL로 보이며 키를 변경/재시작해야 한다. 토큰이나 운영 DB는 Git에 넣지 않는다.

## 6. 남은 구성 요소 및 완료 조건

아래는 **후속 작업 목록**이다. 폴더 정리 과정에서 새 기능을 구현한 것으로 간주하지 않는다.

| 우선순위 | 남은 작업 | 구현/작업 위치 | 완료 조건 | 필요한 외부 정보 |
| --- | --- | --- | --- | --- |
| P0 | ARM64 이미지와 Pi 기본 구동 | 각 Dockerfile, `deploy/` | 6개 컨테이너 기동, UID/볼륨/USB 권한, UDS 연결, 종료·재기동 확인 | Pi OS/장치·현장 네트워크 |
| P0 | 실제 S2E 두 대 수집·좌표 확인 | driver / processing | 30분 동시 스캔, A/B 개별 단절·복구, 실제 각도·단위·설치축 검증 | 모델/펌웨어/IP/장착도 |
| P0 | 현장 보정 구성 | 운영 config + processing | 빈 상태/기준 적재 단계/별도 검증 단계, 양쪽·단일 센서 보정표 승인 | 측량·기준 적재율·승인자 |
| P0 | 백엔드 인계 시험 | Uplink/Heartbeat 계약 | 실제 TLS·인증, 저장 후 ACK, ACK 유실·중복, 30분 단절 후 복원 | 측정/Heartbeat URL·키·서버 담당자 |
| P0 | 카메라→미디어 실연결 | camera-edge + 별도 영상 저장소 | 실제 USB 촬영·인증·수신, 카메라 ID 일치, 전송 지연 계측 | 카메라 모델·WSS·인증 매핑 |
| P1 | 카메라 native 장애 복구 보강 여부 | camera-edge | USB 제거/재삽입·read stall에서 경보/복구가 목표 시간 내 동작; 실패 시 해당 경로 수정 | 허용 중단 시간·하드웨어 |
| P1 | 급변/가림 후보 규칙 검증 | `calibration.candidate_hints` | 실제 사례별 오탐/미탐 기록과 임계치 승인; 없으면 비활성 상태 명시 | 현장 이벤트 표본 |
| P1 | Pi 장시간 부하·운영 관측 | services / 호스트 운영 | 1Hz 처리 지연, CPU/RAM/온도, 8시간 누수/재시작, 디스크 상한 확인 | 운영 부하·전원 조건 |
| P1 | 영상 시간창 종단 연결 | 측정 camera_reference ↔ 영상 서버 | 동기/비동기 조건별 실제 촬영 구간 조회 시험 | 미디어 녹화·조회 API와 백엔드 이벤트 계약 |
| P1 | 릴리스/업데이트·롤백 자동화 | `.github/workflows` 또는 운영 CI, `deploy/` | ARM64 빌드/테스트, 이미지 digest manifest, Outbox 보존 업데이트·롤백 시험 | GitHub org/권한/registry/배포 주체 |
| 조건부 | 복잡한 고정 마스크 | processing | 전체 높이·bin 정렬 사각형 외 형상이 필요할 때만 면적 처리 추가 및 정확도 시험 | 현장 가림 형상 |

P0는 현장 연결을 시작하기 위한 선행 조건이고 P1도 운영 인수 전에 필요한 항목이다.
측정 백엔드나 관제 화면을 직접 만드는 항목은 없다. 영상 녹화·검색 등 네 담당 서버 작업은
저장소 루트에서 형제 경로 `../ajin-camera-media-service/docs/ARCHITECTURE.md`의 남은 요소와 연결한다.

## 7. 배포·GitHub 인계

저장소는 하나, 프로세스와 Docker 이미지는 서비스별이다. 실제 릴리스에는 이미지 digest와
`SITE_ID`, `EDGE_ID`, `CAMERA_ID`, `CONFIG_REVISION`, `DEPLOYMENT_REVISION`, 실행 버전 manifest를
함께 확인한다. 카메라와 드라이버가 모두 동일 Python 패키지 버전이라고 가정하지 않는다.
`.github/workflows/platform-ci.yml`은 플랫폼 검사·wheel·C++ core·Compose 검증을,
`camera-ci.yml`은 기존 카메라 검사와 ARM64 이미지 빌드를 정의한다. 카메라 CI는 서비스 안의
`.github`가 아니라 저장소 루트로 이동했다. 미디어 저장소의 기존 CI는 자체 루트에 유지한다.
GitHub CI 결과는 커밋별 Actions 실행에서 확인한다. 소스 공개와 현장 배포 승인은 별개다.

## 8. 검증 범위

계약·런타임·패키지 경계와 합성 리플레이를 검사한다. 재현 명령과 미검증 항목은
[VALIDATION](VALIDATION.md)에 정리한다. Linux AMD64 SDK 빌드 성공은 ARM64/Pi/실센서 인수를 뜻하지 않는다.
