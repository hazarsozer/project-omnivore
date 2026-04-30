FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 omnivore

WORKDIR /app

COPY --from=builder --chown=omnivore:omnivore /app/.venv ./.venv
COPY --from=builder --chown=omnivore:omnivore /app/src ./src

USER omnivore

CMD ["python", "-m", "omnivore.worker.main"]
