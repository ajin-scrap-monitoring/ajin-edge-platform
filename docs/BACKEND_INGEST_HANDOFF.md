# 백엔드 수신 성능 검토 요청

## 담당 범위와 현재 기준

엣지 담당은 라이다 원본·좌표 전달, A/B 개별 단면과 종합 판단, Pi 내구 큐·전송·설정 API를 관리한다.
백엔드 담당은 수신 검증·저장·투영·DB 트랜잭션과 저장 ACK를 관리한다.
백엔드 구현과 배포 변경은 담당자가 검토·수행한다. 이 문서는 구현을 적용하는 패치가 아니다.

이전 백엔드 PR225의 수신 변경은 원복 PR228로 취소한다. 배포 기준도 변경 전97a380d다.
엣지 PR17의 내구 큐 개선·설정 우선 전송·동시4슬롯 재사용은 유지한다.
이전 엣지+수정 백엔드 조합의 검증 결과를 엣지 단독 개선 결과로 해석하지 않는다.

## 확인한 수신 경로

`src/app/services/edge_v2_service.py::accept_message`는 compact 저장에서
`edge_v2_projection.lock_projection`을 호출한 후 영수증·artifact 저장과 투영을 수행하고 commit 후 ACK한다.
이 잠금은 같은 site/edge의 `edge_v2_filters` 행을 대상으로 한다.

`edge_v2_projection.py::project`는 Measurement의 delivery_kind가 PERIODIC이 아니면
live 투영을 변경하지 않고 반환한다. 따라서 EVIDENCE도 사용하지 않는 투영 행 잠금을 잡는 경로가 있다.
운영 입력의 읽기 전용 관측에서 이 행의 transactionid·tuple 잠금 대기를 확인했다.
이 관측만으로 백엔드만이 원인이거나, 현재 부하에서 수정이 반드시 필요하다고 단정하지 않는다.

## 담당자에게 요청하는 검토

EVIDENCE 수신의 live projection 잠금 생략 여부를 검토해 달라.
PERIODIC·STATE·STATUS의 투영 순서 보호와 기존 live 상태 갱신 잠금은 유지하는 방향이다.
EVIDENCE가 첫 메시지일 때의 초기화, 동일 스캔을 포함하는 다른 메시지와의 동시 저장도 확인해야 한다.
최종 방식과 적용 버전은 백엔드 담당자가 결정한다.

검토 범위는 원본·처리 결과 artifact의 불변성을 유지하면서 불필요한 직렬화를 줄이는 것이다.
원본 점 수·품질·필터·판단 주기·근거 스캔 수를 줄이거나 저장 이전에 성공 ACK하는 방식은 요청하지 않는다.

다음 조건은 계속 보장해야 한다.

- 장치 범위·토큰·스키마 검증, 원래 본문 hash와 message_id 멱등성.
- 같은 message_id의 동일 본문 재전송은 기존 수신 영수증으로 중복 성공.
- 같은 ID의 다른 본문, 같은 artifact 키의 다른 내용은 기존 충돌 규칙으로 거부.
- 원본/처리 artifact unique constraint와 savepoint 재시도 또는 동등한 동시 쓰기 보호.
- 영수증·필수 artifact 저장 commit 후 ACK. 실패 시 롤백과 재시도 가능한 오류 의미 유지.
- EVIDENCE가 최신 PERIODIC·STATE·STATUS와 그 순서를 덮어쓰지 않음.

DB 스키마·보관 기간·화면·높이 계산 정책 변경은 이 요청 범위에 포함하지 않는다.
추가 변경이 필요하다면 백엔드 담당자가 별도 영향 범위를 명시하면 된다.

## 검증 요청

SQLite의 순차 검사만으로 동시 저장의 안전성을 판단하지 않고, 분리된 PostgreSQL에서 독립 세션으로 검사한다.
운영 DB에 시험 레코드를 넣거나 기존 측정값을 수정하지 않는다.

| 상황 | 확인할 결과 |
|---|---|
| 서로 다른 메시지ID, 같은 스캔 원본의 동시 EVIDENCE | 접수 영수증 각각 저장, 같은 원본 artifact 중복·충돌 없음 |
| 동일 메시지ID·동일 본문 동시 재전송 | 최초 저장1회, 나머지는 같은 received_at의 중복 ACK |
| 동일 artifact 키·상충 원본 | 한 내용만 보존, 다른 내용은 충돌로 거부 |
| PERIODIC와 EVIDENCE가 같은 스캔을 동시 참조 | 원본 불변성·live 투영·영수증 일관성 유지 |
| live 메시지의 동시 수신 | 최신 상태·순서 보호 유지 |
| flush·commit 실패, 연결 단절, ACK 유실 | 미저장 성공 ACK 없음, 재전송으로 같은 본문 복구 |

운영 입력 재검증은 같은 엣지 이미지·설정·센서 범위로 실시한다.
EVIDENCE/PERIODIC/STATE별 요청 수, ACK 지연, DB 잠금 대기와 Pi 대기열 추세를 함께 대조한다.
검증 구간·손실 카운터·재시작 세대·시작/끝 대기량을 구분해서 기록한다.
데이터가 쌓이지 않는 것과 모든 수신 간격이 정확히1초인 것은 별도 지표다.

## 연동 형식과 전달 정보

기준 규격은 [v2 OpenAPI](../contracts/backend/v2/openapi.json)와
[measurement 스키마](../contracts/backend/v2/measurements.schema.json),
[assessment 스키마](../contracts/backend/v2/assessments.schema.json)다.
구현 범위는 [v2 구현 설명](V2_IMPLEMENTATION.md)을 참고한다.
v2 ACK는 message_id·received_at·accepted·duplicate를 사용하며,
200 중복 ACK와409 내용 충돌을 구분한다.

원본 measurement의 각도·거리·획득 시각·점별 world XYZ와,
STATE의 `section_profiles[].profile.bins[].height_mm`·단면·종합 결과는 역할이 다르다.
A/B는 서로 다른 단면이므로 같은 bin 번호를 같은 공간 점으로 합치지 않는다.
이미 변환한 XYZ에 설치 R/t를 재적용하거나, 가공 높이에 중앙값·바닥 차감을 다시 적용하면 이중 계산이 된다.
이 수신 성능 요청은 높이·적재율의 의미를 변경하지 않는다.

회신에는 검토 결과·변경 범위·검사 결과·적용 commit/이미지·ACK 의미 변경 여부를 적어 달라.
실제 표본과 운영 주소·인증값은 공개 저장소에 올리지 않고 별도 합의된 경로에서 취급한다.
