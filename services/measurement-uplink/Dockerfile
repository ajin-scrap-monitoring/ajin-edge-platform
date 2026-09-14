FROM python:3.13.11-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.28
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services/lidar-processing/src ./services/lidar-processing/src
COPY services/measurement-uplink/src ./services/measurement-uplink/src
COPY services/edge-orchestrator/src ./services/edge-orchestrator/src
RUN uv sync --frozen --no-dev
USER 10001:10001
ENTRYPOINT ["measurement-uplink"]
