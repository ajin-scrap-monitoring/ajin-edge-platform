# 스캔 갱신 지연 경고

`edge_status.sensors[].scan_update_alarm`은 기존 1초 데이터 최신성과 별도인 운영 경고다. `schema_version: "2.0"`의 선택 필드 확장으로 제공한다. **구버전의 엄격한 스키마에서는 거부될 수 있으므로 지원되는 전송부와 백엔드가 준비된 뒤 활성화한다.**

## 활성화

런타임 JSON의 `scan_update_alarm_enabled`는 boolean이며 기본값은 `false`다. false이면 기존 wire payload만 전송하며, 새 판정은 로컬 `edge-orchestrator.json`의 `scan_update_alarms`에 센서 ID별로 기록한다. true이면 같은 판정을 각 센서의 `scan_update_alarm` 필드에도 포함한다. 오케스트레이터의 상태 변화 보고에는 새 경고 상태 변화도 포함하며, 매번 바뀌는 age만으로 보고를 촉발하지 않는다.

전송 활성화 순서: 수신 스키마·경고 처리 지원 확인 → 확장 스키마를 포함하는 전송부 이미지 적용 → 오케스트레이터 flag=true. 비활성화는 flag=false로 되돌린다. 설정 재적용 시 기존 서비스 재시작 및 손실 경고 정상 시간 재검증 정책이 적용된다. 백엔드 코드와 배포는 해당 담당자의 범위다.

## 필드와 상태

| 필드 | 의미 |
| --- | --- |
| state | UNKNOWN / CLEAR / PENDING / ACTIVE / RECOVERING |
| active | UNKNOWN=null, CLEAR·PENDING=false, ACTIVE·RECOVERING=true |
| scan_age_ms | 엣지 단조 시각 − 마지막 수용된 스캔 획득 단조 시각, ms; 알 수 없으면 null |
| last_valid_scan_at | 마지막 수용된 스캔의 실제 획득 시각; 시작 전 null |
| confirm_after_ms | 3000. 정확히 3000ms는 미확정, 초과 시 확정 |
| reason_codes | CLEAR·UNKNOWN=[], PENDING=[SCAN_UPDATE_DELAY_PENDING], ACTIVE=[SCAN_UPDATE_DELAY_CONFIRMED], RECOVERING=[SCAN_UPDATE_RECOVERING] |

수용된 스캔은 기존 입력 검증과 단조 시각 진행 검사를 통과한 새 scan_id를 뜻한다. 점별 품질·보정·계산 유효성을 보장하는 필드는 아니다. 명시적 입력/서비스 오류를 이 상태로 대체하거나 유예하지 않는다.

- 최초 스캔 전 UNKNOWN이며 정상으로 간주하지 않는다.
- 갱신 지연 ≤1초는 CLEAR, 1초 초과~3초 이내는 PENDING, 3초 초과는 ACTIVE다.
- PENDING에서 새 스캔으로 복귀하면 CLEAR이며 활성·복귀 이벤트를 만들 필요가 없다.
- ACTIVE 후 새 스캔은 RECOVERING으로, **새 스캔 획득 시각이 1초 이상 진행**하고 그 사이 1초 최신성 기준을 지켜야 CLEAR가 된다. 같은 스캔을 반복 조회하거나 벽시계만 경과해서는 해제되지 않는다.
- RECOVERING 중 최신성 1초 초과는 재준비 시간을 초기화하고 active=true를 유지한다. 3초 초과는 ACTIVE다.
- A/B는 독립이다. 중복·역행·미래 시각·null 스캔으로 갱신하지 않는다. 보고 사이에 새 스캔이 왔더라도 실제 3초 초과 간격은 판정한다.
- 임계점 이후 다음 평가에서 보고하므로 수신 시각이 정확히 3초가 되는 것은 아니다.

## 기존 계약과 구분

기존 data_state, last_scan_at, reason_codes, overall_state 및 측정·판단 결과는 유지한다. 1초 초과 시 여전히 STALE이며, 양 센서의 데이터가 오래되면 MEASUREMENT_UNAVAILABLE일 수 있다. 수신 측은 **스캔 지연 활성 알림에만 새 필드를 사용**하고 계산에는 기존 유효성 기준을 유지해야 한다. 필드 누락·UNKNOWN은 정상 복귀로 해석하지 않는다. 다른 오류·손실 경고는 별도 유지한다.

OUTBOX_DATA_LOSS의 연속 정상 1시간 조건도 변경하지 않는다. 짧은 지연이 이 타이머를 초기화할 수 있다. 이번 규격은 모든 판단 불가나 알림을 제거하는 규격이 아니다.
