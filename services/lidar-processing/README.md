# LiDAR processing

실제 Python 코드는 `src/ajin_lidar_processing/`에 있다. 스캔 전처리·좌표변환·품질 판정·
두 센서 융합·보정·후보 힌트와 합성 리플레이를 담당한다.

저장소 루트의 `pyproject.toml`/`uv.lock`으로 설치한다. 이 폴더에서 별도 uv sync를 하지 않는다.
Docker context는 저장소 루트이며 런타임은 독립 프로세스다.

```shell
uv run --frozen lidar-processing --help
uv run --frozen pytest tests/test_processing.py tests/test_processing_runtime.py tests/test_replay.py
```

보정 제약은 [처리 상세](src/ajin_lidar_processing/README.md)에 있다. 현장·단일 센서 보정표,
급변/가림 임계치 승인, Pi 성능·정확도 검증이 남아 있다.
[전체 아키텍처와 완료 조건](../../docs/ARCHITECTURE.md) · [운영 절차](../../docs/OPERATIONS.md)
