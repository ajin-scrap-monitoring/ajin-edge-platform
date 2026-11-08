# 오케스트레이션 개별 단면 및 종합 판단

## 책임과 처리 순서

전처리의 PERIODIC과 판단 근거 EVIDENCE는 선택 구간의 원본 점 순서·index·거리·각도·품질·시각을 유지한다.
오케스트레이션은 사본으로 계산하며, 판단 필터가 제외한 점도 원본 전송에서 삭제하지 않는다.

A/B는 서로 다른 평면을 본다. 같은 bin 번호를 같은 위치로 가정하지 않는다.
A/B 높이를 평균하거나 서로 빈 bin을 채우지 않고, 다른 단면의 높이 차이를 SENSOR_DISAGREEMENT로 처리하지 않는다.

1. 센서별 최근 관측 최대 8개를 보관한다. 인스턴스·시계 변경이나 시간 창을 넘는 공백은 해당 이력을 초기화한다.
2. 각 센서의 최신 스캔을 기준으로 자기 이력 전체에서 단면을 계산한다. 종합할 때 두 최신 기준 시각이
   같은 단조 시계이고 차이가 설정값 이하(최대 100ms)인지 검사한다. 불일치면 각 단면은 유지하고 종합만
   보류한다. 과거의 맞는 쌍을 골라 현재 시각 불일치를 숨기거나 정상 센서 이력을 잘라내지 않는다.
   원본 PERIODIC의 LATEST_TIME_MATCHED_PAIR 선택 정책은 별도로 유지한다.
3. 각 센서의 원본 거리·각도와 검증된 R/t로 실수 좌표를 계산한다. 전송된 정수 좌표를 재입력하지 않는다.
   품질·거리·SDK 무효 사유·자기 단면의 높이 범위를 검사한다.
4. 자기 축의 bin별 스캔 Z 중앙값, 선택적 공간 고립점 필터, 같은 센서의 시간 중앙값을 적용한다.
   최소 유효 스캔 수 미달 bin은 빈 구간이다. 공간 필터는 마스크를 건너지 않는다.
5. 자기 센서의 OBSERVED 양끝 사이에서만 설정 길이 이하의 빈 구간을 선형 보간한다.
   외삽·긴 구간·마스크 경계 보간은 하지 않는다. 다른 센서의 점을 사용하지 않는다.
6. 바닥을 뺀 판단 높이를 0~상한 높이로 제한하고 개별 면적비를 계산한 뒤 종합한다.

## 설정과 기하

설치 보정은 [Pi 보정 API](CALIBRATION_API.md)로 갱신할 수 있다. 백엔드 설정 ACK 후 활성화하며,
획득 revision과 계산 revision을 분리한다. 판단 설정·가중치는 이 API가 생성하지 않는다.
근거 전달은 내부 stage RPC당 최대4개·16MiB로 묶고 각 파일 동기화와 마지막 디렉터리 동기화 뒤에만
저장 성공을 반환한다. 최종 판단은 모든 근거의 ID·해시·센서·revision 검증 후 저장한다.
HTTP 전송은 최대4개 병렬이며 각 메시지의 저장 ACK와 재시도는 독립이다.

판단 모델은 `INDEPENDENT_SECTIONS_V1`이다. sections는 등록된 두 sensor_id 각각의
원점·수평 축·bin 폭·바닥·상한·마스크·필터·보간 한도를 가진다. 축·bin 수·폭·범위·용량·마스크는 달라도 된다.
window는 공통 시간 정책이다. 기본 설치값은 자동 생성하지 않는다.

반사점은 `p = R × [r cosθ, r sinθ, 0] + t`다. 센서 평면 법선 n은 R의 세 번째 열이며
`n · (p − t) = 0`이다. 수평 단위 축 u는 `n_x u_x + n_y u_y = 0`을 만족해야 한다.
거의 수평인 스캔 평면은 높이 단면 계산에 사용할 수 없다.

bin 위치는 `(p_xy − origin_xy) · u`다. 기울어진 평면에서는 같은 bin의 XY 위치가 높이에 따라 바뀌므로
고정 XY 선과의 거리로 정상 점을 제거하지 않는다. Z는 공통 좌표계의 수직 높이다.
profile의 plane_origin_mm과 plane_normal은 자기 센서의 측정 평면을 나타낸다.

```text
센서 s 단면 점유 면적 = Σ 활성 bin 폭 × 판단 높이
센서 s 단면 용량 면적 = Σ 활성 bin 폭 × (상한 − 바닥)
q_s = 단면 점유 면적 / 단면 용량 면적
```

면적은 `area_basis: VERTICAL_PROJECTION`, 즉 수평 폭과 수직 높이의 투영 면적이다.
기울어진 평면 자체 면적이나 전체 부피가 아니다. 여러 표면이 있으면 중앙값을 대표 높이로 쓰며,
보이지 않는 내부 공극·다중 표면을 복원하지 않는다. 미해결 활성 bin이 하나라도 있으면
자기 단면의 전체 면적·면적비는 null이다. coverage_ratio는 보간 전 직접 관측한 활성 bin 비율이다.

## 종합 적재율

fusion.method는 `CALIBRATED_WEIGHTED_SECTIONS`다. 두 component 각각에 sensor_id,
양의 weight와 단조 calibration 곡선을 명시한다. weight 합은 1이고 곡선 입력은 0~1을 모두 덮는다.
출력도 0~1이며 선형 구간 보간한다.

```text
종합 적재율 = Σ weight_s × calibration_s(q_s) / Σ weight_s
```

기본 동일 가중치·기본 보정곡선은 없다. 설치와 적재 상태별 검증으로 설정을 정한다.
설치 보정은 VERIFIED여야 좌표를 사용한다. judgment와 fusion은 VERIFIED 또는 DEFAULT이면 계산한다.
DEFAULT는 기본 설정의 추정치이며 LIMITED·DEFAULT_SETTINGS로 구분한다. UNVERIFIED는 계산 보류를 유지한다.
두 단면으로 전체 부피를 정확히 복원한다고 주장하지 않는다. 예시의 0.25/0.75와 항등 곡선은 합성 시험값이다.

## 백엔드 전달 형식

기존 configurations와 assessments 경로를 사용한다. 새 경로는 없다.
등록 judgment의 새 모델과 판단의 다음 선택적 필드를 수용해야 한다.

- section_profiles: A/B 두 항목. sensor_id, state, reason_codes와 자기 profile을 가진다.
  profile은 기하·bin별 바닥/상한/높이/출처·면적/용량/면적비/coverage·자기 센서 통계를 담는다.
  출처는 OBSERVED / INTERPOLATED / UNAVAILABLE / MASKED다.
- fusion_result: 종합 모델 버전, 센서별 입력 면적비·보정값·가중치·anchor_scan_id,
  pair_delta_ms·clock_domain_id·최종 ratio다. 종합 불가 시 null이다.
- fill_estimate.ratio: 종합 ratio와 동일하다. 백분율 표시는 ratio × 100이다.
  method_version은 종합 모델 버전, rule_version은 전체 판단 설정 버전이다.
- evidence.scans: 개별 계산에 실제 사용한 스캔 참조다. 부분 단면 진단 근거도 포함하며
  계산하지 못한 센서의 근거는 만들지 않는다. 해당 원본 전체는 EVIDENCE로 전달한다.

| 조건 | 개별 단면 | 종합값 및 원인 |
| --- | --- | --- |
| 정상·검증 완료 | 각각 AVAILABLE | 수치 제공, 전체 AVAILABLE |
| 짧은 내부 보간 | 해당 센서 LIMITED | 제공 가능, INTERPOLATED_BINS와 전체 LIMITED |
| 한 센서 미수신·만료 | 정상 센서 유지 | null, INSUFFICIENT_INPUT |
| 최소 스캔 미달 | 해당 센서 UNAVAILABLE | null, INSUFFICIENT_HISTORY |
| 보정 누락·미검증·버전 불일치 | 정상 센서 유지 | null, CALIBRATION_MISSING / CALIBRATION_UNVERIFIED / CALIBRATION_MISMATCH |
| 축과 자기 평면 불일치 | 정상 센서 유지 | null, SECTION_GEOMETRY_MISMATCH |
| 미해결 빈 bin | 부분 profile 유지, 그 센서 UNAVAILABLE | null, SECTION_INCOMPLETE |
| A/B 시간 불일치·다른 시계 | 각각 계산 가능 | null, SENSOR_TIME_MISMATCH |
| 종합 설정 없음·미검증 | 각각 계산 가능 | null, FILL_FUSION_NOT_CONFIGURED / FILL_FUSION_NOT_VALIDATED |
| 전체 판단 설정 미검증 | 모두 보류 | null, FILL_METHOD_NOT_VALIDATED |
| 기본 판단·종합 설정 DEFAULT | 조건을 만족한 단면 계산 | 계산 가능 시 추정값, LIMITED와 DEFAULT_SETTINGS |
| 계산 중 입력 만료 | 결과 보류 | null, INPUT_EXPIRED_DURING_JUDGMENT |

유효 개별 단면만 있으면 전체 decision_state는 LIMITED이며 둘 다 계산 불가하면 UNAVAILABLE이다.
fill capability는 검증된 종합 설정의 활성 상태로 현재 입력 가용성과 구분한다.
가림·수거·영상 탐지는 NOT_IMPLEMENTED다. 높이 차이를 집게 사건으로 만들지 않는다.

기존 단일 section_profile은 과거 기록 파싱용으로 유지한다. 새 출력은 section_profiles와 fusion_result를
함께 쓰며 단일 필드와 섞지 않는다. 기존 공통 단면 설정은 등록 호환만 유지하고
LEGACY_SECTION_MODEL_UNSUPPORTED로 수치 판단을 보류한다. 자동 변환·자동 평균을 하지 않는다.

공개 [설정](../examples/v2/judgment-configuration.json)은 모두 UNVERIFIED다.
[판단 예시](../examples/v2/judgment-assessment.json)는 같은 기하를 합성 시험에서만 VERIFIED로 설정한 결과다.
기하는 공개 시뮬레이터 environment.v1.json의 189c0a6 버전 참고값이며 현장 실측이 아니다.

## 근거 보호와 적용 순서

Bundle begin으로 참조 ID·해시와 판단을 등록해 근거를 보호한다. EVIDENCE는 한 건씩 저장한다.
commit으로 저장 본문·해시·참조를 대조하고 판단을 마지막에 저장한다. 용량 부족으로 자기 근거를
밀어낸 판단만 수락하지 않는다. 중간 실패 시 일부 근거는 남아도 의존 판단은 저장하지 않는다.
비정상 종료 임시 보호는 최대 300초 뒤 해제된다. 원격 HTTP 도착 순서는 보장하지 않는다.
worker 계산, 센서별 이력 최대 8개, 진행 중 묶음 1개·대기 묶음 1개, 묶음당 근거 최대 16개와
판단 1개, 기존 IPC 64MiB 보호·재시도·백오프를 유지한다.

백엔드의 새 설정·출력 수용 확인 후 설치 보정·개별 단면·종합 검증값을 새 config_revision으로 등록한다.
저장 ACK 후 활성화한다. 전송 서비스는 Bundle 지원 버전이어야 한다.
운영 주소·토큰·큐를 예제로 덮어쓰지 않는다. 코드 변경만으로 운영 설정을 자동 활성화하지 않는다.

## 검증

공개 시뮬레이터의 서로 다른 기울어진 두 평면에서 거리·각도를 역산해 시험한다.
A 높이 2m/B 높이 8m, 다른 축·범위·마스크, 센서 누락·시간 불일치, 필터·중앙값·보간 독립,
원본 PERIODIC/EVIDENCE 보존, 미검증·기존 모델 보류, 출력 의미 검사와 세 서비스 전달을 확인한다.
benchmark_section_judgment.py는 센서별 800점·5스캔·128개 bin과 근거 11개 메시지 구성을 검사한다.
합성 정확도·실행 시간은 현장 정확도나 원격 HTTP 저장 처리량 검증과 별개다.

2026-11-08 검증: 로컬 전체 282개, Pi ARM64 격리 Docker 전체 282개 통과.
Pi는 네트워크 차단·읽기 전용 소스·CPU 1개·메모리 512MiB로 제한했다.
동일 제한의 성능 검사 20회는 중앙값 210.948ms, 최대 238.034ms였다.
린트·변경 Python 파일 포맷·패키지 빌드를 확인하고 독립 검토에서 지적된 최신 시간 정합 문제를
회귀 테스트로 재현한 뒤 수정했다. 운영 서비스 설정·이미지는 교체하지 않았다.
