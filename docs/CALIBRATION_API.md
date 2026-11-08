# Pi 라이다 보정 수신 API v1

이 API는 엣지 플랫폼이 제공한다. 설치 보정을 전달하는 호출자는 백엔드 설정 수신 API와 구분하여
Pi의 제어 API를 호출한다. 카메라 미디어 서비스의 API가 아니다.

## 요청

`PUT /api/control/v1/calibrations`

필수 헤더: `Authorization: Bearer <전용 제어 토큰>`, `Content-Type: application/json`,
`Idempotency-Key: <요청 식별자>`. 본문은 최대32KiB이며 압축을 받지 않는다.
같은 키·같은 본문은 같은 작업을 반환한다. 같은 키·다른 본문은409다.

```json
{
  "site_id": "site-example",
  "edge_id": "edge-example",
  "base_config_revision": "installation-r1",
  "config_revision": "installation-r2",
  "coordinate_frame_id": "pit-example-mm",
  "calibrations": [{
    "sensor_id": "lidar-a",
    "calibration": {
      "version": "survey-a-v1",
      "verification_state": "UNVERIFIED",
      "angle_sign": 1,
      "angle_zero_offset_mdeg": 0,
      "rotation": [[1,0,0],[0,1,0],[0,0,1]],
      "translation_mm": [0,0,0]
    }
  }]
}
```

이 예제는 명세 설명용 미검증 값이다. 운영 설치값으로 사용하지 않는다.
한 요청에 등록된 A/B 중1개 또는2개를 전달한다. 누락한 센서의 보정은 유지한다.
배포 기준의 site/edge/좌표계는 변경할 수 없다. 현재 revision이 base와 같아야 하며,
새 config_revision을 사용해야 한다. 센서별 보정 version도 값이 바뀌면 새 버전을 사용한다.

좌표는 `p_world = rotation × [r cosθ, r sinθ, 0] + translation_mm`다.
`θ = angle_sign × angle + angle_zero_offset_mdeg`이며 각도 단위는 milli-degree, 거리·좌표는mm다.
행렬은 오른손 직교 회전이어야 한다. NaN/무한대, 중복 JSON 키, 알 수 없는 필드는 거부한다.
`VERIFIED`는 호출자가 현장 검증을 끝냈다는 선언이다. API가 실측 정확성을 검증하는 것은 아니다.
`UNVERIFIED`면 기존 규칙에 따라 `position_mm` 전체가null이고 추정 z를 채우지 않는다.
보정이 VERIFIED여도 개별 무효 점은 좌표가null일 수 있다.

## 수신과 적용

수신202는 작업의 내구 저장 성공을 뜻한다. 응답 예:

```json
{"accepted":true,"job_id":"survey-request-1","state":"PENDING_BACKEND_ACK",
 "config_revision":"installation-r2","message_id":"configuration-message-id",
 "received_at":"2026-11-08T00:00:00.000Z","reason_code":null}
```

`GET /api/control/v1/calibration-jobs/{job_id}`로 진행 상태를 확인한다. 모든 GET에도 인증이 필요하다.

| 상태 | 의미 |
|---|---|
| PENDING_BACKEND_ACK | 새 edge_configuration을 기존 uplink로 전달하고 백엔드 저장 확인 대기 |
| APPLYING | ACK 뒤 활성 설정을 원자 전환했고 서비스별 적용 확인 중 |
| APPLIED | 현재 boot에서 전처리·uplink·오케스트레이터 모두 새 revision을 최근5초 내 보고 |
| REJECTED | 백엔드 설정 거부. 기존 활성 보정 유지 |
| SUPERSEDED | 이후 설정으로 대체된 작업 |

APPLIED는 입력 정상이나 적재율 정확성의 보증이 아니다. 적용 서비스가 멈추거나 상태 보고가 오래되면
현재 적용 확인은 다시 APPLYING으로 보일 수 있다. PUT 재전송은 APPLIED에200, 나머지 수신 작업에202다.
현재 값은 `GET /api/control/v1/calibrations`에서 확인한다. 이 응답은 보정·revision·pending 작업만
제공하고 토큰·내부 파일 경로·백엔드 주소를 노출하지 않는다.

전처리·판단 서비스는 새 활성 revision에서 이력을 새로 시작한다. 전환 이전 버퍼 스캔은 새 보정에
섞지 않는다. 획득 드라이버의 revision은 별도로 고정하므로 SDK 설정 변경으로 오인하지 않는다.
영속 전송 큐는 보존하고 이전 revision 메시지도 자기 revision을 유지한다.
보정만 받는 API이며 단면 구간·필터·적재율 보정곡선·A/B 가중치를 임의 생성하거나 변경하지 않는다.

## 오류와 보관

| HTTP | 대표 사유 |
|---|---|
| 401 / 403 | UNAUTHORIZED / DEVICE_SCOPE_MISMATCH |
| 409 | BASE_REVISION_MISMATCH, CONFIGURATION_APPLY_IN_PROGRESS, IDEMPOTENCY_CONFLICT, CALIBRATION_VERSION_REUSED |
| 413 / 415 | BODY_TOO_LARGE / JSON_REQUIRED, CONTENT_ENCODING_UNSUPPORTED |
| 422 | INVALID_CALIBRATION_REQUEST, INVALID_CALIBRATION, UNREGISTERED_SENSOR |
| 429 / 503 | CONTROL_BUSY / CONTROL_STORAGE_UNAVAILABLE, CONTROL_CREDENTIAL_UNAVAILABLE |
| 507 | CONTROL_STORAGE_FULL, CONTROL_VERSION_HISTORY_FULL |

전달 경로 단절은 작업 조회의 LOCAL_DELIVERY_UNAVAILABLE로 표시한다. 자동으로 적용 성공 처리하지 않는다.
최신 작업 응답64개, 요청·설정 버전 해시 각128개, 센서 보정 버전 해시256개를 유지한다.
응답이 만료된 키 재사용은 REQUEST_HISTORY_EXPIRED409다. 버전 이력이 차면507이며 자동 삭제하지 않는다.
제어 파일은 최대128KiB, status와 합산한 보조 예산1MiB, 전체 로컬 저장 예산4GiB 안에 포함한다.
디렉터리 저장 확정이 실패하면 활성 상태를 공개하지 않는다. 재시작 후 복구 상태도 저장 확정 성공 뒤에만 공개한다.
초기화 표식 뒤 상태 파일이 소실되면 초기 보정으로 돌아가지 않고 중단한다.
복구 시 상태 파일·초기화 표식·배포 기준을 같은 백업에서 복구하고 임의로 표식을 지우지 않는다.

## 실행과 권한

오케스트레이터 이미지에서 `--entrypoint edge-v2 <image> calibration-api --config /config/runtime.json`으로
별도 컨테이너를 실행한다. runtime에 다음 설정을 추가한다(주소·토큰은 로컬 운영 파일에만 둔다).

```json
{
  "driver_config_revision": "installation-r1",
  "control_dir": "/control",
  "configuration_state_file": "/control/calibration-control.json",
  "calibration_api": {
    "host": "127.0.0.1",
    "port": 8765,
    "token_file": "/run/secrets/calibration_api_token"
  }
}
```

기존 runtime 전체에 합치는 추가 필드다. API를 먼저 실행해 상태를 초기화한 다음 데이터 서비스3개를 시작한다.
전용control 폴더는 API에만RW, 데이터 서비스3개에RO, 카메라에는 마운트하지 않는다.
status·outbox·control은 겹치지 않는 별도 폴더여야 한다. 상태 파일은 숨김이 아닌 .json 이름을 사용한다.
API에는 control 토큰만 제공하고 백엔드 토큰 파일은 마운트하지 않는다.
토큰은 최소32자 ASCII이며 백엔드 token_file과 다른 파일이어야 한다. 내용은 로그·Git에 남기지 않는다.
기본loopback 또는 명시한 Tailscale 인터페이스만 허용한다. 외부접근은 Tailscale 접근제어 내에서 제공한다.
와일드카드·공인 인터페이스 바인딩은 거부하며 일반 인터넷으로 포트를 공개하지 않는다.

JSON Schema: [calibration-request.schema.json](../contracts/control/v1/calibration-request.schema.json).
