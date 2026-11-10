# 전체 범위 점별 필터 XYZ 설계

## 목표와 경계

원본 measurement/EVIDENCE와 A/B 대표 높이를 유지하면서, 각 센서의 현재 선택 각도 범위 전체에 대한
필터 XYZ를 추가한다. 360도 수집 범위를 새로 확장하거나 A/B 점을 합치지 않는다.
백엔드 저장소의 코드·Git·PR·배포 작업은 수행하지 않는다. 수신 규격은 엣지에서 정의해 문서로 전달한다.
운영 백엔드가 새 필드를 수용하기 전에는 새 출력만 비활성으로 유지한다.

## 전달 규격 선택

기존 POST /api/edge/v2/assessments의 STATE에 선택 필드 filtered_point_clouds를 추가한다.
별도 HTTP 요청을 늘리지 않고 A/B 결과를 같은 판단에 연결한다. 기존 필드와 원본 배열은 변경하지 않는다.
이것은 v2의 선택 확장이며, 내부 점별 가공 규격 version은 ANGULAR_RANGE_MEDIAN_V1이다.

- 비활성: 기존 설정과 STATE에 새 필드를 넣지 않는다. 예전 메시지의 canonical 본문도 그대로다.
- 활성: STATE.filtered_point_clouds는 센서별2개 결과 또는 전체 계산 불가 시 null이다.
- 센서 결과: sensor_id, state, reason_codes, coordinate_frame_id, calibration_version,
  verification_state, processing_version, anchor, source_scans, point_count, valid_point_count,
  point_indices[], angles_mdeg[], xyz_mm[], valid[], invalid_reason_codes[], sample_counts[], processing.
- xyz_mm[i]는 [x,y,z] 또는 null이다. mm 단위이며 소수점3자리로 반올림한다.
- 모든 점별 배열은 최신 anchor 스캔의 선택 점 수와 길이·순서가 같다. point_indices는 원래 index다.
- 무효점은 삭제하거나 [0,0,0]으로 채우지 않는다. null, valid=false, 사유와 표본 수를 제공한다.
- anchor는 scan_id·stream_instance_id·sequence·획득 UTC·monotonic·clock domain·원/선택 점 수를 가진다.
- source_scans는 기존 evidence REF 형식이다. 해당 원본은 기존 EVIDENCE로 같이 전달한다.
- 원본의 position_mm에 R/t를 다시 적용하지 않는다. 필터 거리와 anchor 각도로 로컬 좌표를 만든 뒤 R/t를 한 번 적용한다.

수신 측이 추가로 수용할 필드는 configurations.judgment.point_cloud와
assessments.filtered_point_clouds 두 곳이다. 새 endpoint나 측정 API 변경은 요구하지 않는다.
원래 수신기는 엄격한 스키마이므로 수용 전에는 활성화하지 않는다.

## 필터 선택 — 구현 기본값

아래 수치는 구현 기본값으로 선택한 것이며 현장 정확도 검증값을 의미하지 않는다.
대표 높이의4bin ROI·바닥·상한·곡선은 전체 점별 결과에 적용하지 않는다.

1. 센서별 최근 이력과 설치 보정을 사용한다. 시간 창은 기존 judgment.window를 공유한다
   (기본5스캔/최소3/만료1000ms, 최대8개). 별도 무한 이력이나 버퍼 증설은 없다.
2. 각 스캔에서 SDK 무효·좌표 계산 사유·품질10미만·거리1~30000mm 밖의 점을 무효로 한다.
3. 같은 선택 각도 구간 안의 각도순 이웃2개씩을 사용한다. 이웃 최소3개가 있어야 한다.
   이웃 각도 간격500mdeg 초과나 구간 경계를 넘지 않는다. 이웃 거리 폭100mm 이내로 서로 일치할 때만
   그 중앙값과200mm 넘게 차이 나는 고립 점을 무효로 한다. 실제 표면 경계는 이웃 불일치로 보호한다.
4. 최신 스캔의 각도를 기준으로 이전 스캔의 가장 가까운 각도를50mdeg 이내에서 대응한다.
   각 스캔에서 한 원본 점을 여러 anchor 점에 재사용하지 않는다. 충돌은 각도 차이, anchor 순서로 결정한다.
5. 최신 anchor 점 자체가 무효이면 과거 점으로 메우지 않는다. 유효 대응이 최소3개 이상인 경우
   거리 중앙값을 구해 anchor 각도에서 XYZ를 계산한다. X/Y/Z별로 서로 다른 점의 중앙값을 섞지 않는다.
6. 부족한 점은 null로 유지한다. 점 구멍 보간·구간 밖 확장·다른 센서 사용은 하지 않는다.

전체 점 수·센서별 유효 점 수·각 점의 기여 스캔 수와 사유를 제공한다.
빈 입력/미검증 보정/이력 부족/부분 유효를 명시하고, A/B의 시각과 유효성을 독립적으로 표시한다.
가공 결과가 대표 높이·종합값의 의미나 계산 결과를 변경해서는 안 된다.

## 설정과 활성화

기존 judgment 모델에 선택 객체 point_cloud를 추가한다. 생략 또는 enabled=false이면 출력하지 않는다.
필터 수치는 전체 point_cloud 객체로 지정하며 기존 Pi configuration 제어 API로 변경 가능하게 한다.
window는 기존 공통 설정을 따른다. 활성 설정의 revision·불변성·백엔드 저장 ACK·서비스 적용 절차를 유지한다.
GET defaults의 기존 기본 판단에는 새 객체를 자동 삽입하지 않는다.
원복된 기존 백엔드는 이 선택 확장을 아직 지원한다고 가정하지 않는다.
예전 서버의 거부는 기존 설정을 유지하게 하며, 실제 운영 활성화는 담당자의 새 규격 수용 확인 후 수행한다.

## 코드 경계

- point_cloud.py: 센서별 전체 점 필터·대응·XYZ·사유. 원본 입력을 변경하지 않는 순수 계산.
- point_cloud_schema.py: 선택 설정과 결과 스키마 및 결과의 길이·null·참조·수치 불변 조건.
- judgment_schema.py / judgment.py: 선택 설정, 기존 이력 스냅샷으로 계산하고 근거 원본의 합집합 유지.
- orchestration.py / contracts.py: 활성 시 STATE 선택 필드 추가와 검증. 기존 경로·큐·동시4슬롯 유지.
- contracts/backend/v2 및 contracts/control/v1: 엣지 소유 JSON Schema/OpenAPI 갱신.
- docs와 examples: 수신자가 그대로 구현할 필드·기본값·실패·활성화 계약과 명확히 표시한 합성 예제.

## 검증과 배포 경계

무효점·고립 이상치·표면 경계·각도 지터·일대일 대응·범위 경계·SDK0거리·시간 만료·센서 재시작·보정 버전 변경을 검사한다.
원본 본문과 기존 대표 높이가 활성/비활성 비교에서 변하지 않아야 한다.
원본 EVIDENCE 참조 완결성, 과거 스키마 호환, 새 배열 길이·null/valid·유한 수·불변성도 검사한다.
비지원 수신기의 설정 거부와 기존 송신 유지, 지원 mock 수신기의 새 STATE 수신을 확인한다.
전체 회귀·린트·패키지·독립 검토·CI·Pi ARM64 Docker 검증을 수행한다.
가능한 경우 Pi의 실제 입력은 격리된 검증 수신기로만 검사한다. 운영 백엔드·설정·큐는 변경하지 않는다.
새 출력은 기본 비활성이며, 수신 측 준비 전 실제 백엔드에 신규 필드를 보내지 않는다.

## 검토 후 확정한 경계

기여 최소 수는 고정3이 아닌 공통 window.min_scans다. sample_counts는 anchor를 포함한 서로 다른 유효 대응 스캔 수다.
공간 이웃은 중심점을 제외한 사전 유효점으로 동시에 판정한다. 대응 충돌의 패자는 다른 후보를 재시도하지 않는다.
각도 동률은 anchor의 원래 순서와 원본 index 순으로 정한다. 0/360 경계와 선택 구간 경계를 이어 붙이지 않는다.
근거는 동일한 공유 이력에서만 합친다. 기존 단면 profile이 존재하면 해당 센서의 이력 전체를 이미 사용하므로
scan_count와 근거 수의 기존 일치 검사를 완화하지 않는다. profile이 없는 센서의 점별 전용 근거만 추가될 수 있다.
대표 높이 계산과 기존 만료 판정·generated_at은 먼저 확정한다. 이후 점별 계산이 늦어져도 기존 결과를 지우지 않는다.
점별 계산에만 별도 완료 시점 만료 검사를 적용해 숫자를 무효 처리하며 사유를 해당 점별 결과 안에 표시한다.
STATE의 기존 종합 사유에 점별 사유를 합치지 않는다. 신규 processing 객체에는 설정과 공유 window를 그대로 명시한다.
anchor가 없으면 anchor=null·빈 점 배열·UNAVAILABLE이다. 보정 없음은 calibration_version=null과 사유로 표시한다.
