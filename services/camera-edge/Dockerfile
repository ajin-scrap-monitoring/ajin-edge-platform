FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cameraedge

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv
RUN uv sync --frozen --no-dev

USER cameraedge

CMD ["/app/.venv/bin/python", "-m", "camera_edge.main"]
