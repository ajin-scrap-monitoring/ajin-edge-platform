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

## SDK 수신 큐 정리 보정

고정 SDK의 `AsyncTransceiver::unbindAndClose`는 `new Buffer()`로 생성한 큐 항목에
`delete[]`를 사용한다. 잔여 수신 큐를 정리할 때 메모리 오류가 발생하므로 CMake가
원본 SHA-256을 확인한 뒤 해당 해제를 `delete`로 바꾼 복사본을 컴파일한다.
SDK checkout은 변경하지 않으며, 원본이 달라지면 빌드를 중단한다. 라이선스 고지는 유지한다.

전체 Docker 빌드는 `sdk_cleanup_test`에서 빈 큐·단일 항목·다중 항목과 반복 disconnect를
검사한다. `AJIN_CORE_ONLY=ON`은 SDK를 빌드하지 않으므로 이 회귀 검사를 포함하지 않는다.
메모리 진단 시 CMake C++/링커 옵션에 `-fsanitize=address -fno-omit-frame-pointer`를
설정하고 `ASAN_OPTIONS=detect_leaks=1`로 테스트를 실행한다.
