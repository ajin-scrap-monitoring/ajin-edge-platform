ARG PYTHON_IMAGE=python:3.13.11-slim-trixie@sha256:2b9c9803c6a287cafa0a8c917211dddd23dcd2016f049690ee5219f5d3f1636e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.28@sha256:59240a65d6b57e6c507429b45f01b8f2c7c0bbeee0fb697c41a39c6a8e3a4cfb

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services/lidar-processing/src ./services/lidar-processing/src
COPY services/measurement-uplink/src ./services/measurement-uplink/src
COPY services/edge-orchestrator/src ./services/edge-orchestrator/src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_REVISION=unknown
ARG SOURCE_VERSION=development
LABEL org.opencontainers.image.source="https://github.com/ajin-scrap-monitoring/ajin-edge-platform" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${SOURCE_VERSION}" \
      org.opencontainers.image.title="ajin-edge-platform lidar-processing"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
ENTRYPOINT ["lidar-processing"]
