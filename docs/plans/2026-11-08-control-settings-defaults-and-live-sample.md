# 통합 설정 API·기본값·실제 스캔 회신 구현 계획

> 기존 worktree에서 직접 구현한다. 실패 회귀→구현→검증 순서로 진행하며 완료 전 독립 코드 검토를 수행한다.

## 요구와 설계

설치 보정·센서별 단면·종합 보정곡선·가중치를 백엔드가 Pi 제어 API로 변경한다.
현재 인증, config_revision 충돌 검사, 멱등성, 내구 저장, 백엔드 설정 ACK 후 활성화와 서비스 적용 확인을 유지한다.
실제 전송 스캔의 점3개 발췌 JSON과 설명 MD만 별도 회신 폴더에 만든다. 실데이터는 공개 Git에 넣지 않는다.

- 기존 `/api/control/v1/calibrations`는 설치 보정 전용으로 호환 유지한다.
- `GET/PUT /api/control/v1/configuration`은 활성 설치 보정과 전체 judgment를 조회·갱신한다.
  calibrations와 judgment는 선택 사항이며 최소 하나를 전달한다. 미전달 항목은 유지한다.
  judgment는 원자적으로 전체 교체하고 null은 판단 비활성화다. 수신 주소·토큰·센서 목록·좌표계 변경은 받지 않는다.
- `GET /api/control/v1/configuration/defaults`는 현재 시뮬레이터용 기본 프로필을 제공한다.
  알려진 frame/sensor에만 제공하며 다른 장치에 임의 설치 좌표를 적용하지 않는다.
- `GET /api/control/v1/configuration-jobs/{id}`는 기존 작업 조회와 같은 ACK·적용 상태를 제공한다.
- 통합 요청은128KiB 할당 제한, 기존 보정 요청은32KiB를 유지한다. 제어 상태128KiB·보조1MiB·전체4GiB 한도는 유지한다.
- 기본 설치 pose는 공개 시뮬레이터와 정합 검증한 R/t를 사용한다. A/B 개별 단면은 검증한 내부1m 영역,
  250mm×4bin, 바닥0mm·상한10000mm, 기존 필터·5스캔 중앙값·최대100ms 정합 설정을 사용한다.
- 기본 보정곡선은 A/B 모두 `[[0,0],[1,1]]`, 가중치는0.5씩이다.
  judgment/fusion의 DEFAULT 상태를 추가하고 기본값의 계산 결과는 LIMITED·DEFAULT_SETTINGS로 표시한다.
  UNVERIFIED는 기존처럼 계산 보류하고 DEFAULT를 현장 검증 완료로 위장하지 않는다.
- 현재 실제 백엔드는 judgment와 새 결과 필드를 거부한다. 전체 판단 기본값은 API 템플릿으로 준비하고,
  실제 운영에는 우선 기존 백엔드가 수용하는 설치 기본값을 적용한다. 전체 판단은 ACK 전 활성화하지 않는다.

## 작업

1. [ ] DEFAULT 결과·항등곡선·동일 가중치·미검증 보류 및 프로필 경계 회귀 실패 재현 후 기본 프로필과 판단 지원 구현
   - `packages/edge-common/src/ajin_edge/v2/settings_defaults.py`, `judgment_schema.py`, `judgment.py`
   - `tests/test_v2_settings_defaults.py`
2. [ ] 설치·단면·곡선·가중치 원자 갱신, 기존 보정 API 호환, 인증·잘못된 범위·ACK 거부·재시작 회귀 작성 후 구현
   - `calibration_schema.py`, `configuration_state.py`, `calibration_api.py`
   - `tests/test_v2_configuration_control.py`, `tests/test_v2_configuration_api.py`
   - 허용된 judgment만 mutable로 추가하며 기존 버전·장치 식별·source revision 검증은 유지
3. [ ] 통합 API 명세·요청 스키마·기본 프로필 예제 갱신, 전체 회귀·린트·패키지·독립 검토·CI 확인
   - `docs/CONFIGURATION_API.md`, `contracts/control/v1/configuration-request.schema.json`
   - 기존 v2 계약 export의 DEFAULT 상태 반영
4. [ ] Pi Docker의 실제 입력으로 모든 설정 API 변경·재시작·원본 좌표·단면·종합 기본 추정 검증
   - 검증된 이미지로 데이터 서비스3개와 API를 교체하며 기존 큐·인증·설정을 보존
   - 시뮬레이터 환경 정합 확인 후 설치 기본값을 실제 API로 적용하고 백엔드 저장 ACK 확인
5. [ ] 실제 백엔드 수신 payload에서 같은 스캔의 점3개만 서버 측 발췌, JSON+MD 두 파일 작성
   - 메시지·센서·스캔 식별자, 시각, 원래 index·거리·각도·품질·XYZ·유효성 값을 유지
   - 원문 점 수를3으로 고치지 않고 발췌 사실과 생략 범위를 MD에 설명
   - z는 반사점 세계 좌표이며, bin height_mm은 floor를 뺀 판단 높이임을 구분
   - null·0 의미, 시각 출처, 센서별 평면, 실제 전달 경로와 각 서비스·설정 버전을 명시
   - 인증 정보·운영 주소·전체 원본 점 배열은 회신·Git에 포함하지 않는다

## 검토 초점

부분 설정 누락으로 기존 값 소실, 모델 DEFAULT를 VERIFIED로 오표시, 잘못된 센서/행렬/가중치/곡선 수락,
ACK 전 활성화, 재시작 시 수정된 judgment 거부, 발췌본을 전체 원문이나 새 합성값으로 오인하는 경우를 검증한다.
