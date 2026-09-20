# v2 실행 및 검증

v1 실행 명령·DB·운영 배포는 변경하지 않는다. v2는 `edge-v2` 명령으로 명시적으로 선택한다.
이 경로는 개발·통합 검증용이다. 운영 승인은 실제 보정 입력과 Pi 컨테이너 시험 후 별도로 한다.

## 역할

| 프로세스 | 입력 | 출력 |
| --- | --- | --- |
| 드라이버 `--schema-version 2.0` | SDK 완성 스캔 | 수집 ID·UTC·boot 단조 시각·원본 순서 |
| `edge-v2 processing --config /config/runtime.json` | 드라이버 A/B UDS | 모든 관측 스트림, 1초 정기 측정 |
| `edge-v2 orchestrator --config /config/runtime.json` | 처리 관측·서비스 상태 | 1초 판단, 10초 및 변화 시 상태 |
| `edge-v2 uplink --config /config/runtime.json` | 두 서비스의 UDS 요청 | 네 HTTPS API, 저장 ACK·격리·재시도 |

카메라 영상은 기존 카메라 엣지→미디어 서비스 경로를 유지한다.
영상 탐지 결과 참조 형식은 정의했지만 모델·스트림 메타데이터 확장을 구현했다고 주장하지 않는다.

`runtime.json`에는 `configuration`(등록할 전체 v2 JSON), 센서 ID→UDS 주소의 `drivers`,
`delivery_endpoint`, `observation_endpoint`, `storage_dir`, `status_dir`, `clock_domain_file`,
`backend_origin`, `token_file`을 넣는다. 프로덕션은 서로 다른 절대 경로 UDS만 허용한다.
boot 파일은 `/proc/sys/kernel/random/boot_id`, 저장 경로는 v1과 다른 전용 디렉터리다.
실제 URL·주소·토큰·현장 설정은 Git 밖에서 관리한다.

등록 JSON은 `contracts/backend/v2/configurations.schema.json`에 따른다.
예시 설치값을 자동 생성하지 않는다. 새 revision마다 새 message_id를 부여하고 재시작 시에는
동일 등록 파일을 재사용한다. 등록은 서버 저장 ACK 후에만 정상 출력을 활성화한다.
현재 실행기는 시작 시 설정을 읽는다. 실행 중 설치 변경은 서비스 중지·새 설정 등록 후 재시작하며,
이전 보정으로 계속 운전하지 않는다. 게이트의 설치 무효화 API를 hot reload로 오해하지 않는다.

## 계약 및 판단

- 공개 JSON Schema와 OpenAPI: `contracts/backend/v2/`.
- `validate_message`는 점 수·원본 index·쌍 시각 등 JSON Schema만으로 표현하기 어려운 의미도 검증한다.
- 정수 mm의 정확한 절반은 0에서 먼 쪽으로 반올림한다. 변환 중간 단계는 실수다.
- 보정 미검증/미입력 시 null 좌표, 품질값으로 점을 제거하지 않는다.
- SDK HQ quality 원본 byte(0~255)를 유지한다. dist_mm_q2=0은 SDK_INVALID_RANGE와
  null 좌표로 전달한다. 정수 mm 환산 후 0이 되었다는 이유로 제거하지 않는다.
  근거: [고정 SDK HQ 정의](https://github.com/Slamtec/rplidar_sdk/blob/99478e5fb90de3b4a6db0080acacd373f8b36869/README.md),
  [SDK 무효 거리 처리](https://github.com/Slamtec/rplidar_sdk/blob/99478e5fb90de3b4a6db0080acacd373f8b36869/sdk/src/sl_lidar_driver.cpp#L118).
- 적재율·가림·수거·집게 판단 알고리즘은 활성화하지 않는다. 현재 STATE는
  `fill_estimate=null`, `FILL_METHOD_NOT_IMPLEMENTED`, 빈 사용 근거를 보고한다.
- 사건 생성·개정 및 EVIDENCE 생성 인터페이스는 있지만, 실측 검증된 규칙 연결 없이 사건을 생성하지 않는다.
- 시간 동기 상태는 관측 근거 없이 SYNCED로 꾸미지 않고 UNSYNCED로 보고한다.
  chrony 등 실제 오프셋 수집 연결은 운영 전 검증 대상이다.

## 내부 통신

드라이버는 기존 protobuf 서비스에 scan_id/clock_domain_id를 추가한 하위 호환 스키마를 사용한다.
schema_version 1.0 기본값은 유지하며 v2 소비자는 1.0 프레임을 거부한다.

v2 서비스 사이에는 UDS gRPC의 UTF-8 JSON 메서드를 사용한다:

- `/ajin.edge.v2.Delivery/Enqueue`: 전체 본문 → 로컬 대기 저장 상태. 원격 ACK가 아니다.
- `/ajin.edge.v2.Delivery/Inspect`: `{message_id}` → `remote_acked`, 상태.
- `/ajin.edge.v2.Delivery/Stats`: `{}` → 실제 저장·손실 집계.
- `/ajin.edge.v2.Observations/Subscribe`: `{}` → 관측과 누락 카운터 스트림.

관측 구독 버퍼는 구독자당 8개, 처리 최근 버퍼는 센서당 32개다.
전송 프로세스에 연결하지 못할 때 각 생산자의 메모리 대기는 10개이며 넘치면 손실을 보고한다.
이를 무손실 영구 큐라고 간주하지 않는다. 로컬 UDS 성공 이후에만 디스크 내구성을 보장한다.

내부 gRPC 64MiB는 메모리 보호 한도이며 승인되지 않은 백엔드 HTTP 크기 제한이 아니다.
범위를 줄이거나 점을 버려 맞추지 않는다. 실제 최대 스캔 JSON 크기와 Pi 처리량을 시험해야 한다.

## 저장·운영 제한

전용 v2 저장소는 단일 writer의 fsync+atomic rename 파일 큐다. v1 SQLite/WAL은 열거나 변환하지 않는다.
본문뿐 아니라 레코드·메타데이터·교체 임시 파일을 포함해 쓰기 전 공간을 예약한다.
논리 길이와 파일 할당량 중 큰 값을 합산하고 일반 데이터와 별도로 진단 쓰기 여유를 둔다.
기본 한도는 4GiB, 시간 기준 자동 만료는 없다. 공유/활성 스캔 참조를 보호한다.
인증 오류는 토큰 파일 내용 변경까지 전송을 멈추며 수집은 계속한다.

파일시스템 전체 quota, Pi의 실제 할당 단위·디스크 고갈·전원 차단 시험은 별도다.
상태 파일 및 atomic 교체 여유 1MiB를 전체 4GiB 안에서 별도 예약하고 실제 상태 파일
할당량을 used_bytes에 합산한다. 상태 폴더는 전용으로 사용한다. 다른 프로그램의 무제한 로그를
같은 폴더에 쓰지 않는다. 메모리 버퍼는 별도 유한 개수 제한을 사용한다.
실제 Pi 파일시스템 quota와 전원 차단 시험까지 완료했다는 뜻은 아니다.

## Docker

기존 세 Python 서비스 Dockerfile은 공통 패키지를 모두 포함하므로 `edge-v2`도 포함된다.
새 이미지로 별도 검증 compose를 만들 때 entrypoint만 `["edge-v2", "processing"]`,
`["edge-v2", "uplink"]`, `["edge-v2", "orchestrator"]`로 각각 지정하고
command에 `--config /config/runtime.json`을 제공한다. UDS·상태 경로를 공유하고
저장소는 uplink만 쓰도록 mount한다. 드라이버에는 `--schema-version 2.0`을 명시한다.
v1 compose와 같은 UDS/저장 경로로 동시에 실행하지 않는다.

릴리즈 워크플로는 기존 이미지 빌드·테스트 게이트를 유지한다. 이 변경으로 tag 발행·push·Pi 배포를
자동 수행하지 않는다. 카메라 이미지와 v1 릴리즈 동작을 임의 변경하지 않는다.

## 로컬 검증

`pytest -q`는 v1 회귀와 v2 계약/처리/전송/저장/판단/3서비스 통합 테스트를 실행한다.
`python tools/export_schemas.py`로 공개 계약을 재생성한다.
통합 시험의 드라이버와 HTTPS 수신자는 합성 시험 구성이다. 실장비나 팀원 백엔드 연동 통과와 다르다.

### 2026-09-20 검증 기록

- 플랫폼/v2 회귀209개, 기존 카메라51개 통과. lint 통과.
- 소스 배포본과 wheel 빌드 통과.
- 설정 ACK 이후 측정·판단·상태가 전달되는3서비스 통합 시험 통과.
- 전송 중 정리, ACK/격리, 만료 참조, Retry-After, 상태/데이터 전송 공정성,
  관측 손실 보고, 느린 상태 RPC 중 판단 주기 회귀 시험 통과.
- 문서의 전체 JSON 예시4개를 현재 계약 검증기로 확인했다.
- 합성32770점 본문 약4.6MB. 점 검증 경로 최적화 후 Windows 측정 약122ms.
  이 수치는 Pi 성능·네트워크 요구량 보장이 아니다.
- 로컬 Docker 엔진 미실행으로 이번 변경의 C++/Docker/실센서 시험은 미실시.
  GitHub push·PR·릴리즈·Pi 배포는 수행하지 않았다.
