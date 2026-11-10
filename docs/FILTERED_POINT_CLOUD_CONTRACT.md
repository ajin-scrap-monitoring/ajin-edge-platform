# 전체 점별 필터 XYZ 수신 규격

규격: ANGULAR_RANGE_MEDIAN_V1 / v2 선택 확장. 원본 measurement/EVIDENCE와 기존 대표 높이를 유지한다.
이 문서는 신규 가공 XYZ의 수신 규격이다. 이전 백엔드 성능 수정 요청과는 별개이며, 백엔드 구현·Git·PR·배포는 엣지 작업 범위에 포함하지 않는다.

## 전달 위치

기존 `POST /api/edge/v2/assessments`, `message_type=edge_assessment`, `assessment_type=STATE`에
선택 필드 `filtered_point_clouds`를 추가한다. schema_version은2.0이며 새 HTTP 경로나 별도 전송 요청을 만들지 않는다.

| 데이터 | 기존/추가 | 위치 |
|---|---|---|
| 원본 점 | 유지 | measurements의 sensors[].scan.points[] |
| 대표 높이 | 유지 | STATE.section_profiles[].profile.bins[].height_mm |
| 가공 점별 XYZ | 추가 | STATE.filtered_point_clouds[].xyz_mm[] |

비활성 상태에서는 새 필드를 생략한다. 활성 상태에서 전체 판단 입력이 검증되지 않으면 null,
계산을 시도했으면 센서별2개 객체를 제공한다. 센서 하나가 없거나 무효여도 다른 센서 결과를 채워 넣지 않는다.

## 센서 결과 객체

| 필드 | 형식·의미 |
|---|---|
| sensor_id | A/B의 실제 센서ID. 배열 순서 대신 이 값을 사용 |
| state | AVAILABLE / LIMITED / UNAVAILABLE |
| reason_codes | 센서 결과의 제약·사유 |
| coordinate_frame_id / calibration_version | 좌표계와 설치 보정 버전. 보정 없으면 버전null |
| verification_state | VERIFIED / DEFAULT / UNVERIFIED. 기본 추정과 검증 완료를 구분 |
| processing_version | ANGULAR_RANGE_MEDIAN_V1 |
| anchor | 최신 기준 스캔의 ID·stream·sequence·획득 UTC/monotonic·clock 정보·점 수. 스캔이 없으면null |
| source_scans[] | 실제 검토한 원본 스캔 REF. STATE.evidence.scans에 포함되며 원본은 EVIDENCE로 전달 |
| point_count | 기준 스캔의 selected_point_count |
| valid_point_count | 유효 XYZ 수 |
| point_indices[] | 기준 스캔에서의 원래 점 index |
| angles_mdeg[] | 기준 각도. 1000mdeg=1도 |
| xyz_mm[] | 각 점의 [x,y,z] 또는null. mm, 소수점3자리 |
| valid[] | 같은 위치의 XYZ 유효 여부 |
| invalid_reason_codes[] | 점별 사유 배열. 유효점은[], 무효점은사유1개 |
| sample_counts[] | 같은 각도에 대응한 서로 다른 유효 스캔 수. anchor 포함. anchor가 무효이면0 |
| processing | 이번 결과에 적용한 point_cloud 설정과 공통 window |

점별 배열6개는 길이와 순서가 같다. `xyz_mm[i]`는 `point_indices[i]`가 가리키는 기준 스캔 점의 가공 좌표다.
무효점을 삭제하거나 [0,0,0]으로 채우지 않는다. null과 valid=false를 함께 사용한다.
유효 좌표가 실제 [0,0,0]일 수 있으므로 숫자0을 무효 마커로 사용하지 않는다.
점별 XYZ는 반사점 좌표이며, 바닥 상대 가공 높이인 height_mm와 구분한다.

전체 범위는 센서에 이미 설정된 선택 각도 범위다. 대표 높이의 내부1m/4bin 범위로 잘라내지 않으며,
추가로360도 전송 범위를 확장하지도 않는다. A/B는 독립된 스캔 평면이며 점을 섞지 않는다.

## 계산 순서와 기본 설정

필터의 기본값은 구현 선택값이다. 현장 정확도 검증값은 아니며 기존 판단 설정의 검증 상태를 표시한다.

1. SDK 무효·계산 사유·품질·거리 범위를 검사한다.
2. 같은 선택 각도 구간에서 각도순 이웃의 거리를 비교한다. 중심점을 제외한 사전 유효점으로 동시에 판정한다.
   가까운 이웃끼리 일치하는데 중심점만 튀는 경우를 무효 처리한다. 이웃 자체가 불일치하는 표면 경계는 유지한다.
3. 최신 anchor의 각도를 기준으로 과거 스캔의 가장 가까운 각도를 대응한다. 한 원본 점은 한 anchor에만 대응한다.
   충돌은 각도 차이→anchor 순서→원본 index 순으로 결정하고 패자의 다른 후보 재시도는 하지 않는다.
   0/360 경계·선택 구간 경계·큰 각도 공백을 이어 붙이지 않는다.
4. 유효 대응 거리가 window.min_scans 이상이면 거리 중앙값을 구한다.
5. 중앙값 거리와 anchor 각도로 로컬 좌표를 만든 후 설치 R/t를 한 번 적용해 XYZ를 계산한다.
   이미 world 좌표인 원본 XYZ에 설치 보정을 다시 적용하지 않는다.

최신 점이 무효이면 과거 점으로 메우지 않는다. 원본 index 자체를 시간 대응 키로 쓰지 않는다.
새 점을 보간해 만들거나 다른 센서의 값으로 채우지 않는다.

| 설정 | 기본값 |
|---|---:|
| quality_min | 10 |
| distance_min_mm / distance_max_mm | 1 / 30000 |
| spatial_radius_points | 2 |
| spatial_min_neighbors | 3 |
| spatial_max_deviation_mm | 200 |
| spatial_neighbor_spread_mm | 100 |
| max_adjacent_angle_gap_mdeg | 500 |
| angle_match_tolerance_mdeg | 50 |

시간 창은 기존 judgment.window를 공유한다. 기본5스캔·최소3스캔·만료1000ms이며 최대 이력8개를 유지한다.
기존 버퍼·대기 묶음·4GiB 저장 상한을 늘리지 않는다.

## 상태와 무효 사유

유효점이 없으면 UNAVAILABLE이다. 모든 점이 유효하고 설정이 VERIFIED이면 AVAILABLE,
부분 유효 또는 DEFAULT이면 LIMITED다. DEFAULT_SETTINGS는 기본 설정 상태를 알린다.

주요 점별 사유는 SDK_INVALID_RANGE, SOURCE_INVALID, LOW_QUALITY, OUT_OF_RANGE,
SPATIAL_OUTLIER, INSUFFICIENT_HISTORY, FILTER_UNVERIFIED, CALIBRATION_MISSING, CALIBRATION_UNVERIFIED,
CALIBRATION_MISMATCH, COORDINATE_OUT_OF_RANGE, INPUT_EXPIRED_DURING_FILTER다.
스캔 없음은 NO_SCAN, 선택 점 없음은 NO_SELECTED_POINTS, 전체 무효는 NO_VALID_POINTS로 구분한다.

기존 대표 높이의 결과·사유는 유지한다. 점별 계산만 만료되면 그 결과의 숫자와 사유만 무효 처리한다.
STATE.generated_at은 기존 판단 스냅샷 시각이며 도착 시각이 아니다. 각 점 배열의 기준 시각은 anchor.acquired_at이다.
A/B anchor 시각은 각 객체에서 별도로 확인하며 같은 메시지라는 이유로 동시 측정으로 간주하지 않는다.

## 수용 및 활성화

수신자가 추가로 수용할 선택 필드는 다음 두 곳이다.

1. `POST /api/edge/v2/configurations`의 `judgment.point_cloud`.
2. `POST /api/edge/v2/assessments`의 `filtered_point_clouds`.

이 두 확장의 본문 보존·조회까지 지원한 뒤 엣지에서 활성화한다.
설정만 받고 STATE를 거부하거나 필드를 저장하지 않는 상태는 수용 완료가 아니다.
기존 엄격한 수신 스키마가 새 필드를 지원한다고 가정하지 않는다.
현재 운영은 새 객체가 없는 기존 설정을 유지하며, 새 규격의 수용 전에는 신규 송신을 켜지 않는다.

설정 변경은 기존 Pi `PUT /api/control/v1/configuration`을 사용한다.
GET 결과의 judgment 전체를 복사해 point_cloud 객체를 추가하고 새 config_revision으로 요청한다.
생략은 기존 판단을 유지하는 제어 규칙이 있으므로 해제 시에는 judgment 전체에서 point_cloud를 제거해 요청한다.
설치 보정은 변경하지 않으며 백엔드 저장 ACK 후 적용되는 기존 절차를 따른다.
거부되면 새 설정은 REJECTED이고 이전 활성 설정이 유지된다. 인증값과 운영 주소는 별도 경로로 관리한다.

## 원본 참조·전송·ACK

source_scans는 기존 sensor_id·scan_id·config_revision·calibration_version·coordinate_frame_id 조합이다.
참조의 원본은 measurement/EVIDENCE에 포함한다. 최대8개×2센서의 기존 근거 묶음 상한을 유지한다.
신규 배열을 EVIDENCE 원본의 position_mm에 덮어쓰지 않는다.
기존 4개 대표 높이는 필터 XYZ 배열에서 다시 계산한 값으로 바꾸지 않는다.

기존 Idempotency-Key=message_id와 저장 후 ACK를 유지한다. 새 필드도 본문 hash에 포함된다.
최초201·중복200을 구분하며, 전송 도착 순서와 EVIDENCE 선도착은 보장하지 않는다.
수신 규격의 정확한 타입·범위는 엣지 소유 JSON Schema를 기준으로 한다.

- [STATE 스키마](../contracts/backend/v2/assessments.schema.json)
- [설정 스키마](../contracts/backend/v2/configurations.schema.json)
- [Pi 제어 스키마](../contracts/control/v1/configuration-request.schema.json)

- [합성 STATE 예제](../examples/backend/v2/filtered-point-cloud-state.synthetic.json): 센서별3점의 완전한 메시지.
- [합성 설정 예제](../examples/backend/v2/filtered-point-cloud-configuration.synthetic.json): point_cloud를 포함한 완전한 설정.

예제는 운영 실수신 표본이 아니다. 설치 기하·필터 수치는 현장 설정으로 대체해야 한다.
