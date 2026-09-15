# ARM64 이미지 릴리스

## 범위

`release-images.yml`은 lidar-driver, lidar-processing, measurement-uplink,
edge-orchestrator, camera-edge 이미지 5종을 Linux ARM64로 빌드한다.
카메라 미디어 서버는 별도 저장소의 배포 대상이며 이 매트릭스에 포함하지 않는다.
이미지 생성과 실제 디바이스 업데이트는 분리한다. 워크플로에는 SSH나 자동 재배포가 없다.

## 실행 조건

- PR 또는 수동 실행: 테스트, SDK AddressSanitizer, 이미지 빌드와 기본 실행 검사만 수행한다.
- `main`에 포함된 커밋에 `vMAJOR.MINOR.PATCH` 태그를 push하면 같은 검사를 통과한
  이미지가 GHCR에 게시된다. 실제 릴리스 번호는 담당자가 결정한다.
- ARM64 네이티브 GitHub runner를 사용한다. 다른 CPU 아키텍처 이미지는 생성하지 않는다.
- 릴리스 태그는 이동하거나 재사용하지 않는다. 조직의 tag ruleset으로 수정·삭제를 제한한다.
  이미 GitHub Release가 존재하면 재빌드를 중단하며 기존 첨부 파일을 덮어쓰지 않는다.

## 비공개 패키지

이미지 이름은 `ghcr.io/ajin-scrap-monitoring/ajin-<service>`이다.
패키지를 먼저 private으로 준비해야 하며, private이 확인되지 않으면 게시를 중단한다.
SHA 이미지 게시 직후에도 private 상태를 다시 확인한 뒤 버전 태그를 게시한다.
패키지 접근 권한을 확인할 수 없는 오류는 실패로 처리한다. 404도 신규와 권한 부족을 구분할 수
없으므로 허용하지 않는다. 조직 관리자는 첫 릴리스 전에 5개 이름의 private 패키지를
민감한 내용이 없는 초기 이미지로 준비하고 Actions 접근 권한을 부여해야 한다.
새 GHCR 패키지는 기본 private이지만 이 기본값만 믿고 실제 서비스 이미지를 먼저 올리지 않는다.
공개 저장소의 코드·워크플로 로그·릴리스 메타데이터와 이미지의 공개 범위는 별개다.

조직 설정에서 private package 생성과 해당 저장소의 Actions 쓰기를 허용해야 한다.
기존 패키지는 Package settings의 Manage Actions access에서 이 저장소를 허용한다.
게시에는 job 범위의 `GITHUB_TOKEN`을 사용하며 저장소에 PAT를 넣지 않는다.
디바이스의 pull 인증은 별도의 최소 권한 `read:packages` 자격 증명을 호스트에만 보관한다.
실주소, 토큰, 인증서 개인키, 운영 설정, 센서 데이터는 이미지 빌드 컨텍스트에 넣지 않는다.

## 결과물과 배포

각 이미지는 `sha-<40자리 커밋>`과 버전 태그를 갖는다. `latest`는 발행하지 않는다.
5종 모두 성공해야 GitHub Release에 `release-manifest.json`과 `images.env`를 첨부한다.
불변 릴리스에 대응해 초안을 생성하고 두 파일을 첨부한 뒤 공개한다.
첨부 실패 시 초안 상태로 남기며, 공개된 릴리스의 파일·태그는 변경하지 않는다.
부분 실패 시 일부 SHA 이미지가 남을 수 있지만 완전한 릴리스로 취급하지 않는다.
`images.env`는 Compose의 기존 `*_IMAGE` 변수에 SHA256 digest를 지정하므로 태그 변경의
영향을 받지 않는다. 운영 환경 파일과 병합할 때 운영 값이 덮이지 않도록 확인한다.

배포 담당자는 릴리스의 commit·플랫폼·이미지 5종을 확인하고, 기존 digest와 설정을 보관한 뒤
별도 유지보수 창에서 pull 및 교체한다. SQLite Outbox와 설정은 컨테이너 외부 볼륨에 유지한다.
롤백 전에는 DB 스키마·설정 호환성과 미전송 데이터 보존을 확인한다. 자동 롤백은 구현하지 않는다.

## 검증 한계

카메라 이미지 시작 검사는 실제 모듈 진입점과 종료 정리를 실행하되 장치와 전송 연결을
대체한다. 실제 USB·WSS, 라이다 센서 연결, 현장 보정, 장시간 부하, 운영 서버 인증·멱등 ACK는
현장 인수 항목이다. GHCR 게시 성공은 첫 승인된 릴리스 태그 실행 후 별도로 확인해야 한다.

공식 참고: [GHCR 인증 및 패키지 공개 범위](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[GitHub-hosted runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
