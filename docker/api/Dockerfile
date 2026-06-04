FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# System libs needed at runtime. libmagic1 is required by python-magic for MIME
# sniffing on upload; the media libs are needed because registry.discover()
# imports every handler module at startup. Mirrors docker/worker/Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 omnivore

WORKDIR /app

COPY --from=builder --chown=omnivore:omnivore /app/.venv ./.venv
COPY --from=builder --chown=omnivore:omnivore /app/src ./src

USER omnivore

EXPOSE 8000

CMD ["uvicorn", "omnivore.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
