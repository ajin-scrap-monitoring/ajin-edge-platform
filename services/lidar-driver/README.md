# LiDAR driver

공식 SDK를 사용하는 C++ 수집 서비스다. 같은 이미지를 A/B 센서별 독립 프로세스로 실행한다.
실제 코드는 `src/main.cpp`, 공통 검증은 `tests/core_test.cpp`, 빌드 정의는 `CMakeLists.txt`다.

저장소 루트에서 빌드하며 Docker context도 저장소 루트다:

```shell
docker build -f services/lidar-driver/Dockerfile -t ajin-lidar-driver:0.1.0 .
```

Proto는 `../../contracts/lidar/v1`에 있다. SDK SHA/라이선스는 [NOTICE](NOTICE)에 있다.
실제 센서 IP/장착축/UDP 동시 수집·복구, ARM64/Pi 검증이 남아 있다.
전체 계약·완료 조건은 [아키텍처](../../docs/ARCHITECTURE.md), 실행 환경은
[운영 문서](../../docs/OPERATIONS.md)를 참고한다. 빌드 명령은 운영 배포를 뜻하지 않는다.
