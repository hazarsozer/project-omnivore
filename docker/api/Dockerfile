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

RUN useradd -m -u 1000 omnivore

WORKDIR /app

COPY --from=builder --chown=omnivore:omnivore /app/.venv ./.venv
COPY --from=builder --chown=omnivore:omnivore /app/src ./src

USER omnivore

EXPOSE 8000

CMD ["uvicorn", "omnivore.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
