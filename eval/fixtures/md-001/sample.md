# Omnivore Documentation

Welcome to the Omnivore project documentation. This guide covers architecture and usage.

## Architecture

The ingestion pipeline processes documents through a series of extraction stages.

### API Layer

FastAPI provides the REST API for file uploads and document status queries.

### Worker Layer

ARQ workers pull jobs from Redis and execute format-specific handlers on each document.

## Getting Started

Clone the repository and install dependencies with uv sync.

Run the infrastructure stack:

```bash
docker compose up -d postgres redis minio
alembic upgrade head
```

You are now ready to upload documents and query results.
