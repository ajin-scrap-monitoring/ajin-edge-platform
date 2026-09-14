# 배포 및 현장 검증

## 1. 현장값 준비

64-bit Raspberry Pi OS/Ubuntu, Pi 5, 센서 전용 네트워크, S2E 고정 IP 2개, USB MJPEG 카메라를
확인한다. 실제 주소·endpoint·키를 예제에 자동 대입하지 않았다.
`processing.example.json`을 별도 운영 설정으로 복사하고 측량한 회전/이동·ROI·바닥/높이·보정표를
작성한다. 보정 버전과 config_revision은 내용이 바뀔 때 새로 발급한다.

기본 bin 폭은 50mm. ROI 다각형/마스크와 bottom/max_height_mm 배열은 같은 현장 좌표를 사용해야
한다. 측정각은 SDK 좌수계에서 부호 변환하며 sensor→site는 오른손 강체변환이다.
품질 threshold 및 각도/거리 필터는 processing/README.md를 참고한다.
현재 고정 polygon mask는 bin 경계에 정렬된 전체 높이의 사각형 컬럼 제외만 지원한다.
부분 컬럼/임의 다각형 마스크는 보간으로 메우지 않고 calibration 오류로 거부한다.
`calibration.candidate_hints`를 명시하면 이동하는 좁은 구조물/coverage 급감 후보는
OCCLUSION_SUSPECTED, 유효 fill의 급상승/급감은 RAPID_RISE_SUSPECTED 및
COLLECTION_DROP_SUSPECTED로 보고한다. 임의 형상 추적이나 확정 수거 이벤트가 아니다.
window_seconds(최대 2초), 비율 변화량, 구조물 높이·폭·연속 이동량은 현장 보정값이다.
세 연속 프로파일의 같은 방향 이동 또는 직전 유효 측정 대비 coverage 하락을 판정하며,
가림 후보는 DEGRADED로 표시하지만 후보만으로 반사점을 삭제하지 않는다.
INVALID/시간창 만료 뒤에는 이전 fill을 비교 기준으로 사용하지 않는다.
필드를 생략하면 후보 판정은 비활성화된다. 예제 값은 승인된 현장 기준이 아니다.
실측이 없는 운영 보정표를 생성하거나 demo 플래그만 꺼서 운영 승인하지 않는다.

## 2. 이미지와 호스트 준비

프로젝트 상위가 아니라 `ajin-edge-platform` 저장소 루트에서 실행하는 예:

```shell
docker build --platform linux/arm64 -f services/lidar-driver/Dockerfile -t ajin-lidar-driver:0.1.0 .
docker build --platform linux/arm64 -f services/lidar-processing/Dockerfile -t ajin-lidar-processing:0.1.0 .
docker build --platform linux/arm64 -f services/measurement-uplink/Dockerfile -t ajin-measurement-uplink:0.1.0 .
docker build --platform linux/arm64 -f services/edge-orchestrator/Dockerfile -t ajin-edge-orchestrator:0.1.0 .
docker build --platform linux/arm64 -t ajin-camera-edge:0.1.0 services/camera-edge
python3 tools/prepare_runtime.py --root /opt/ajin/runtime
```

이 명령은 인계용이며 현재 호스트에서 Docker 이미지 빌드를 실행 성공한 것은 아니다.
SDK는 고정 SHA, Python은 uv.lock으로 잠근다. Debian apt와 base image tag는 가변이므로 실제
릴리스에서 빌드한 OCI 이미지의 digest를 기록하고 Compose 환경변수에 image@sha256:…를 넣는다.
같은 소스에서 bit-for-bit 재빌드를 보장하지 않는다. 오프라인은 docker image save/load와
이미지 digest/설정 SHA256 manifest를 함께 전달한다.

prepare_runtime는 새 전용 디렉터리만 준비하며 비밀키나 보정값을 생성하지 않는다.
Linux에서는 관리자 권한으로 실행해야 uid/gid 10001 소유권 설정을 적용할 수 있다.
중간 경로에 symlink를 쓰지 않는다. 호스트의 secret 파일은 UID 10001이 읽을 수 있게 최소 권한을
설정한다(소유자/그룹, 예: 0440). Compose file secret은 파일 bind mount이므로 이미지의 UID와
호스트 권한이 맞아야 한다. USB video 장치의 호스트 group ID를 VIDEO_GID에 지정한다.

호스트에서 chrony를 운영하고 `tools/export_clock.py`를 실행한다. 제공 systemd 예제는
`/opt/ajin/ajin-edge-platform`, uid10001을 전제로 한다. exporter는 chrony monitoring만 읽으며
NTP 서버 설정이나 시스템 시간을 바꾸지 않는다. exporter/chrony 실패·파일 stale은 UNSYNCED다.

## 3. Compose 설정 검증과 실행

`edge.env.example`을 별도 현장 파일로 복사한다. `SITE_ID`, `EDGE_ID`, `CONFIG_REVISION`,
`DEPLOYMENT_REVISION`, `CAMERA_ID`는 JSON과
동일해야 한다. CONFIG_SHA256에는 **운영 JSON 파일 바이트의 SHA256**을 넣는다.
JSON의 service_versions는 6개 서비스 전부의 실행 버전을 기록한다(드라이버 1.0.0,
Python/카메라 0.1.0). 릴리스별 이미지 digest도 별도 보관한다. 버전/배포/카메라 매핑
불일치는 CONFIG_ERROR다. 체크섬·JSON 오류 상태는 검증된 환경 identity로 FATAL을 기록하며,
종료 뒤에도 명시적 CONFIG 진단을 유지한다(diagnostic_stale=true는 살아 있다는 뜻이 아니다).
환경 identity도 잘못됐으면 신뢰할 상태를 생성할 수 없으므로 시작 로그를 확인한다.
설정 endpoint는 `/sockets/a/lidar-a.sock`, `/sockets/b/lidar-b.sock` 기준이다.

```shell
docker compose --env-file /opt/ajin/config/edge.env -f deploy/compose.edge.example.yaml config --quiet
docker compose --env-file /opt/ajin/config/edge.env -f deploy/compose.edge.example.yaml up -d
docker compose --env-file /opt/ajin/config/edge.env -f deploy/compose.edge.example.yaml ps
```

사전 compose 파싱은 서비스가 실제로 기동됐다는 검증이 아니다. 샘플 IP는 문서용 대역이고
endpoint는 `.invalid`다. 운영 환경값 없이는 전송할 수 없다.
Driver는 host networking, 나머지는 bridge다. 이 설정 자체가 센서망 방화벽을 만드는 것은
아니므로 호스트 라우팅/방화벽으로 접근 범위를 별도 제한한다.
컨테이너는 non-root/read-only/cap-drop, 자기 상태 폴더만 RW, orchestrator는 상태 루트 RO다.
오케스트레이터에 Docker socket을 주지 않는다.

## 4. 장애 동작

- 센서 재접속: 독립 1~30초 backoff. 마지막 스캔은 2초 초과 시 폐기.
- processing→uplink: 최대 10개 메모리 대기, 동일 ID 재시도, overflow 오래된 항목 폐기/카운터.
- uplink→server: durable Outbox, 1~60초 backoff, 30초 연속 성공 뒤 backoff 초기화.
- 401/403: Uplink는 종료/재시작 폭주 대신 FATAL로 남아 저장을 계속한다. 비밀키 교체 후 해당
  컨테이너만 재시작한다. 잘못된 서버 payload 계약은 격리 카운터/사유로 확인한다.
- 설정/보정 오류는 측정을 차단한다. 수정한 설정 hash/revision을 맞춰 관련 컨테이너를 재기동한다.
- event-loop stall은 별도 thread watchdog이 15초 후 프로세스 종료한다. 센서가 없지만 정상적으로
  재시도하는 상태는 hang이 아니다.
- Compose healthcheck는 모니터링용이지 자동 재시작 트리거가 아니다. `on-failure:5`는 짧은 실패의
  restart 폭주를 제한한다. 장시간 실행 후 반복 장애에는 호스트 경보/운영자 조치가 필요하다.
- 기존 PyAV 장치 open 실패/장시간 native read, USB 재연결은 현장에서 확인해야 한다. 이번 변경은
  카메라 네이티브 드라이버 재설계를 포함하지 않는다. status stale/frame stale을 모니터링한다.

Outbox 파일을 임의 삭제하면 미전송 데이터가 유실된다. 키 교체·업데이트 시 runtime/outbox를
유지한다. 컨테이너 로그는 5MiB×3개로 제한한다. 원시 스캔과 영상은 Outbox에 넣지 않는다.
재시도 대기는 monotonic 시간으로 계산하고 재시작 시 저장 UTC deadline은 최대 60초로
재설정한다. 보존 24시간은 프로세스가 실행된 monotonic 누적 시간이다. 정지 시간은
보존 나이에 더하지 않으며 UTC 보정만으로 데이터를 폐기하지 않는다. crash 직전 미체크포인트
시간은 보존 나이를 줄일 수 있다. 구버전 DB 최초 전환도 정지 시간을 추정하지 않는다.
oldest_pending_unix는 수신 UTC, oldest_pending_age_seconds는 이 누적 나이다.
first_lost_id/last_lost_id와 loss_range_start_unix/end_unix는 재시작 뒤에도 유지되는
전체 손실 레코드의 수신 UTC 범위이며, 시각 보정이 있으면 측정 순서를 뜻하지 않는다.

## 5. 현장 인수 Gate (미실행)

- [x] WSL Ubuntu 24.04 AMD64 SDK+gRPC 전체 빌드, ctest 1/1 및 무장치 UDS/SDK_RETRY/SIGTERM 확인
- [ ] ARM64 컨테이너 기동/권한 확인
- [ ] S2E 2대 30분 동시 수집, UDP 소켓/주소 충돌 및 실제 scan_hz 확인
- [ ] A/B 개별 단절·복구, 두 센서 단절, 카메라 단절, orchestrator 재시작 중 상호 영향 확인
- [ ] 승인된 단일센서 보정표로 2초 내 DEGRADED 전환, stale fill 없음
- [ ] Pi CPU/RAM/온도/전원/대역폭, 실제 샘플수에서 1Hz 처리, 8시간 누수·restart 관찰
- [ ] 서버 30분 단절 및 복구, ACK 유실 중복 제거, Outbox 최대 건수/물리 파일 사용량
- [ ] chrony 동기/시각 점프, 카메라 실제 촬영·수신 지연, 영상 검색 시간창 검증
- [ ] 고정 적재 상태 20회 반복의 P95 범위 ≤5 percentage points (초기 목표)
- [ ] 보정에 사용하지 않은 현장 단계에서 평균 절대 오차 ≤10 percentage points (초기 목표)
- [ ] 백엔드 이벤트/영상 보존/대시보드 종단 통합 및 담당자 승인
