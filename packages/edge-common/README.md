# Shared edge Python code

`src/ajin_edge`는 설정·상태·시간·JSON 검증·HTTPS·생성된 Proto를 공유한다.
독립 실행 서비스가 아니며 별도 Dockerfile이 없다. 루트 pyproject/lock으로 세 Python 서비스와
함께 wheel에 포함된다. 서비스 코드가 이 폴더 안에 숨겨져 있지는 않다.

계약의 원본은 루트 `contracts/`이며 생성은 `tools/generate_contracts.py`를 사용한다.
[아키텍처](../../docs/ARCHITECTURE.md)에서 패키지/프로세스 경계와 남은 검증을 설명한다.
