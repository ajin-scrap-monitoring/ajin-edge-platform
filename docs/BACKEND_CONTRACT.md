# 백엔드·미디어 인계 계약 v1.0

이 문서는 구현한 클라이언트의 **합의 제안**이다. 실제 서버 endpoint와 인증정책 합의는 미완료다.

## 측정 수신

`POST <MEASUREMENT_URL>` (HTTPS, 인증서 검증 필수)

- `Authorization: Bearer <edge-token>`
- `Content-Type: application/json`
- `Idempotency-Key: <measurement_id>`
- 본문: `contracts/backend/v1/measurement.schema.json`, 최대 64KiB.
- UTC RFC3339 시각, mm 단위, 비율 0~1 및 백분율 0~100. 두 값은 일치해야 한다.
- UUID 형식의 생성 ID를 사용하지만 서버 계약에서는 불투명 문자열로 취급한다.
- `INVALID`는 `fill_ratio`, `fill_percent`가 없으며 최신 유효값으로 대체해 저장하지 않는다.
- `measurement_cycle_id`, 센서별 sequence/instance_id, config_revision, calibration_version으로
  재시작·재전송·보정 변경을 구분한다. 수신 순서와 측정 순서는 다를 수 있다.

서버는 `measurement_id`에 고유 제약을 두고 **저장 커밋 이후** 다음 응답을 반환해야 한다.

```json
{"measurement_id":"요청과 동일한 ID", "accepted":true}
```

정상은 2xx. 이미 저장된 동일 요청은 위 2xx 응답 또는 아래 409 응답으로 확인할 수 있다.
다른 본문으로 같은 ID를 보내면 성공으로 인정하면 안 된다.

```json
{"measurement_id":"요청과 동일한 ID", "duplicate":true}
```

| 결과 | Edge 동작 |
| --- | --- |
| 2xx + 일치하는 accepted=true | Outbox 제거, 제한된 중복 확인 기록 유지 |
| 409 + 일치하는 duplicate=true | 저장 확인과 동일 |
| 응답 유실/잘못된 ACK/408/429/5xx | 같은 ID 재전송, 1~60초 지수 backoff + jitter |
| Retry-After | 초/HTTP 날짜 지원, 최대 60초 제한 |
| 401/403 | FATAL 상태 유지, 새 측정은 계속 Outbox 저장, 운영자 키 교체/재시작 필요 |
| 그 외 3xx/4xx | 재시도 루프 방지: 해당 ID/해시/사유 격리, 손실 카운터 증가 |

리다이렉트는 따르지 않는다. 응답 본문도 64KiB로 제한한다. 키를 URL 쿼리나 로그에 넣지 않는다.
전송은 **at-least-once**이며 ACK 유실 때 재전송이 발생한다. 중복 없는 효과는 서버 멱등 처리로
보장해야 하며 클라이언트 단독 exactly-once를 주장하지 않는다.

로컬 gRPC OK는 SQLite FULL 동기화 커밋 완료이지 원격 저장 완료가 아니다.
Outbox: 최대 100,000건, payload 192MiB, 24시간. DB 물리 상한 240MiB와 WAL checkpoint를 별도
적용하며 256MiB 이상 여유 공간을 확보하고 Pi에서 물리 사용량을 검증한다. 파일 시스템
전체 디스크 quota를 이 코드가 설정하지 않는다. 오래된 데이터부터 폐기하며 손실 카운터가
재시작 후에도 유지된다. 격리 기록은 본문 대신 ID/해시/사유만 최대 1,000개 보관한다.
서버가 장기간 정지하면 데이터 손실은 가능하며 heartbeat에서 드러난다.
보존 나이는 실행 중 monotonic 누적 시간이며 정지 시간과 UTC 보정은 더하지 않는다.
oldest_pending_unix/oldest_pending_age_seconds 및 전체 손실의 first_lost_id/last_lost_id,
수신 UTC 범위 loss_range_start_unix/end_unix를 서비스 상태로 보고한다.
총 전송 상한은 10건/s, 매초 최신 측정 우선 슬롯 1개, 나머지는 오래된 pending 순서다.

## Heartbeat

`POST <HEARTBEAT_URL>` 동일 Bearer/TLS, 본문 `edge-heartbeat.schema.json`. 저장 본문 ACK 없이
2xx를 수신 성공으로 취급한다. 약 10초마다 또는 의미 있는 상태 변경 시 전송한다.
전송 실패 시 backoff 후 **가장 최신 snapshot만** 재전송한다. 별도 Outbox는 없다.

종합 상태: HEALTHY / DEGRADED / MEASUREMENT_UNAVAILABLE / OFFLINE_BUFFERING / CONFIG_ERROR.
서비스 상태: STARTING / HEALTHY / DEGRADED / RETRYING / FATAL / UNKNOWN.
30초 지난 상태 파일, 잘못된 서비스 ID/시각은 UNKNOWN으로 처리한다.
예외로 검증된 identity의 FATAL CONFIG 진단은 diagnostic_stale=true로 유지하여
CONFIG_ERROR를 보존한다. 이는 서비스 생존 확인이 아니다.
deployment_revision/camera_id와 각 서비스의 site_id/deployment_revision/service_version을
포함하며 배포 manifest 및 카메라 매핑과 다르면 CONFIG_ERROR다.
한 센서나 카메라 장애는 다른 데이터 경로의 종료 조건이 아니다.

## 카메라와 시각

카메라의 기존 Binary JPEG/WSS payload는 변경하지 않는다. `camera_reference`는
`camera_id`, `window_start`, `window_end`로 영상 검색 구간을 표현한다.
clock offset ≤100ms는 ±2초, ≤500ms는 ±3초와 CLOCK_DEGRADED,
초과/미확인은 camera_reference 생략 + CLOCK_UNSYNCED.
이는 검색 상관관계이지 센서와 프레임의 하드웨어 동기화가 아니다.
미디어 수신 시각과 실제 촬영 시각의 차이는 별도로 측정해야 한다.

현재 Edge는 서버의 확정 이벤트 ID를 만들지 않는다.
현장 보정 candidate_hints를 활성화하면 OCCLUSION_SUSPECTED,
RAPID_RISE_SUSPECTED, COLLECTION_DROP_SUSPECTED가 quality.reason_codes에 나타난다.
이들은 확정된 사업 이벤트가 아닌 후보 힌트다.
서버 측 임계/수거 판정, Media Service의 해당 시간창 녹화·보존 API와 대시보드 연결은 후속 통합 범위다.
