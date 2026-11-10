# Pi 설치·단면·종합 설정 API

백엔드가 Pi의 설치 보정, A/B 단면, 보정곡선과 가중치를 변경하는 인증된 제어 API다.
기존 설치 보정 전용 API와 같은 전용 Bearer 토큰, 멱등성, 저장 ACK·적용 확인 절차를 사용한다.

## 경로

| 메서드·경로 | 내용 |
|---|---|
| GET /api/control/v1/configuration | 현재 활성 설치 보정·judgment·revision·대기 작업 조회 |
| GET /api/control/v1/configuration/defaults | 알려진 시뮬레이터의 기본 프로필 조회. 조회만으로 적용하지 않음 |
| PUT /api/control/v1/configuration | 설치 보정과 판단 설정의 원자 변경 요청 |
| GET /api/control/v1/configuration-jobs/{id} | 저장 ACK·서비스 적용 상태 조회 |

기존 `/calibrations`, `/calibration-jobs/{id}` 경로도 유지한다. 기존 PUT은 설치 보정만 변경한다.
모든 경로에 `Authorization: Bearer <전용 제어 토큰>`이 필요하다.
PUT에는 `Content-Type: application/json`, `Idempotency-Key: <요청 식별자>`를 전달한다.

## PUT 본문과 갱신 범위

공통 필수 필드: site_id, edge_id, base_config_revision, config_revision, coordinate_frame_id.
현재 base revision을 기준으로 새 config_revision을 지정한다. 같은 revision을 재사용하지 않는다.

| 선택 필드·내부 경로 | 변경 내용 |
|---|---|
| calibrations[].sensor_id / calibration | 등록된 A/B 중 하나 또는 둘의 설치 보정 |
| judgment.sections[] | 센서별 원점·축·bin 폭·바닥·상한·활성 마스크·필터·보간 한도 |
| judgment.window | 시간 중앙값 스캔 수·최소 스캔·입력 만료·A/B 시간 정합 한도 |
| judgment.point_cloud | 선택 범위 전체 점별 필터 XYZ 활성화·품질·거리·공간·각도 대응 설정. [수신 규격](FILTERED_POINT_CLOUD_CONTRACT.md) 참조 |
| judgment.fusion.components[].calibration | 단면 면적비→종합 입력값 보정곡선 |
| judgment.fusion.components[].weight | 센서별 종합 가중치 |

calibrations와 judgment 중 최소 하나를 전달한다. 생략한 항목은 유지한다.
judgment를 전달할 때는 **전체 judgment 객체**를 교체한다. 일부 필드만 보낼 때 기존 필드가 사라지는
암묵적 병합을 하지 않는다. 현재 GET 결과에서 필요한 값을 바꾸어 전체 객체를 PUT하면 된다.
`judgment: null`은 판단을 비활성화한다. 설치 보정은 유지한다.
같은 요청에 설치 보정과 judgment를 함께 보내면 한 설정 revision으로 적용한다.

센서 목록·각도 선택 구간·좌표계·드라이버 source revision·수신 주소·토큰·저장 경로는 변경 대상이 아니다.
좌표계 ID가 다른 설치로의 이전은 별도 배포 작업이다.

전체 본문 예: [default-settings.request.json](../examples/control/v1/default-settings.request.json).
요청 Schema: [configuration-request.schema.json](../contracts/control/v1/configuration-request.schema.json).
예제의 site/edge와 base/new revision은 실제 조회 결과에 맞춰 바꾼다.
GET defaults의 profile_id는 설명용 메타데이터이며 PUT 본문에 넣지 않는다.

## 입력 검증

- 설치 보정: 기존 [설치 보정 API](CALIBRATION_API.md)의 유한 수·오른손 직교 회전·버전 불변성 검사 유지.
- 단면: 등록된 A/B 각각1개, 단위 축, 같은 길이의 바닥·상한·마스크, 상한>바닥, 유효한 bin/필터 범위.
- 가중치: 둘 모두 양수이고 합이1이어야 한다. 자동 정규화하거나 센서 하나를 조용히 제외하지 않는다.
- 곡선: 최소2점, X는0에서1까지 엄격히 증가하고 Y는0~1의 단조 값이다. 중간은 선형 보간한다.
- 시간: 최근 이력 최대8개·입력 만료 최대1000ms·A/B 정합 최대100ms를 유지한다.
- 알 수 없는 키·중복 JSON 키·NaN/무한대·잘못된 장치·기준 revision 충돌을 거부한다.

통합 본문 할당 제한은128KiB, 기존 설치 보정 본문은32KiB다. 압축은 받지 않는다.
이는 제어 요청의 할당 한도이며 라이다 점 제거·백엔드 측정 데이터 상한이 아니다.
제어 상태는 기존128KiB, status와 합한 보조 저장은1MiB, 전체 저장은4GiB 한도를 유지한다.
유효한 요청도 기존 상태와 합쳐 예산을 넘으면507로 거부하고 활성 설정을 유지한다.

## 기본값과 정확도 상태

현재 내장 프로필은 `synthetic-scrap-pit-v1` 및 lidar-a/b 전용이다.
실행 중 시뮬레이터 환경과 설치 pose가 일치하는지 확인한 뒤 사용한다. 다른 현장에 자동 적용하지 않는다.

- 설치: 공개 시뮬레이터의 센서별 R/t, 각도 부호1·오프셋0. 좌표 단위mm, Z=0은 시뮬레이터 바닥.
- 개별 단면: A는 X=1000~2000mm, B는 Y=1000~2000mm의 내부 진단 영역이다.
  250mm×4bin, 바닥0mm·상한10000mm이며 용기 전체 단면을 뜻하지 않는다.
- 필터: quality_min10, 거리1~30000mm, 높이 여유30mm, 공간 반경1bin·편차200mm, 최대 내부 보간1bin.
- 시간: 최근5스캔·최소3스캔, 만료1000ms·정합100ms.
- 곡선: 둘 모두 `[[0,0],[1,1]]`, 가중치: A/B 각각0.5.

판단과 종합 설정의 verification_state는 다음 의미다. 설치 보정의 기존 VERIFIED/UNVERIFIED 규칙은 유지한다.

| 상태 | 동작 |
|---|---|
| VERIFIED | 검증된 설정으로 계산. 실제 입력 품질·시간·완결성 검사도 통과해야 수치 제공 |
| DEFAULT | 기본값으로 추정 계산. 전체 판단은 LIMITED, DEFAULT_SETTINGS 사유를 포함 |
| UNVERIFIED | 기존처럼 계산을 보류. DEFAULT와 동일하게 취급하지 않음 |

DEFAULT는 두 단면을 종합한 추정 지표이며 실제 전체 부피의 정확도 인증이 아니다.
기본 판단 영역에서 계산한 단면도 DEFAULT_SETTINGS로 표시한다. 빈 구간·시간 불일치·센서 누락 검사는 유지한다.
곡선이나 가중치를 바꾸어도 config_revision을 새로 지정해야 한다. 설정 전체는 해당 revision으로 추적한다.

## 저장과 적용

202는 요청의 내구 저장 성공이다. 곧바로 적용 완료를 의미하지 않는다.
`PENDING_BACKEND_ACK → APPLYING → APPLIED` 순서로 진행한다.
기존 uplink가 새 전체 edge_configuration을 보내고 백엔드 저장 ACK를 확인한 뒤 활성 상태를 원자 교체한다.
전처리·전송·오케스트레이터3개가 같은 boot에서 새 revision을 최근5초 내 보고해야 APPLIED다.
APPLIED는 센서 정상이나 계산 성공·정확도 인증을 뜻하지 않는다.

백엔드가 거부하면 REJECTED이며 기존 활성 설정을 유지한다. 재시작·중복 요청·부분 적용·저장 확정 실패는
기존 보정 API와 같은 규칙을 따른다. 제어 폴더 쓰기 권한은 API만 가지고 데이터 서비스는 읽기 전용이다.
새 제어기와 judgment 변경을 읽는 데이터 서비스3개는 이 API 지원 버전으로 함께 배포한다.

현재 연동 백엔드가 judgment/section_profiles/fusion_result를 수용하기 전에는 전체 판단 설정을 활성화할 수 없다.
설치 기본값만 먼저 등록해 원본 XYZ를 제공할 수 있으며, 전체 기본 프로필은 조회·변경 요청용으로 준비된다.
외부 백엔드의 DB 구현은 이 API 작업 범위에 포함하지 않는다.

Pi 실제 입력·운영 적용 결과와 제한: [검증 기록](CONFIGURATION_API_VALIDATION_2026-11-08.md).
