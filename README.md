# Omnivore — self-hosted document ingestion pipeline for RAG

Omnivore is a Postgres-native document ingestion pipeline for RAG systems. Upload any file — PDF, Word doc, spreadsheet, audio recording, scanned image — and get back structured intelligence stored directly in PostgreSQL: chunked text with embeddings, extracted tables queryable as SQL, named entities, language-detected content, and LLM-generated summaries. No separate vector database required.

It is designed to be cloned and run on your own infrastructure. There are no hosted services, no calling home, and no vendor dependencies beyond the models you choose to run.

## Features

| Feature | Details |
|---|---|
| Document formats | PDF, DOCX, TXT, MD, HTML, JSON, CSV, TSV, XLSX |
| Audio transcription | MP3, WAV, M4A, FLAC, OGG, WebM — via faster-whisper |
| Video transcription | MP4, MOV, MKV, AVI, WEBM, OGV — ffmpeg + faster-whisper |
| Image OCR | JPEG, PNG, WEBP, TIFF, BMP, GIF — via EasyOCR (multilingual) |
| Embeddings | BGE-base-en-v1.5 (768-dim), runs fully local via sentence-transformers |
| Hybrid search | BM25 + pgvector + RRF merge via `POST /v1/search` |
| Structure-first chunking | Section boundaries respected; 512-token max, 64-token overlap |
| Named entity recognition | People, orgs, locations, dates — spaCy en_core_web_sm |
| LLM summaries | Abstractive summary per document — pluggable provider (Anthropic / OpenAI / Google / Ollama); opt-in per tenant |
| Vision enrichment | Semantic captions for images and video frames — runs alongside OCR/STT; covers non-text images (photos, diagrams); opt-in per tenant |
| Language detection | Per-chunk language detection via lingua |
| Routing policies | Declarative per-tenant rules controlling which sinks receive chunks |
| Multi-tenancy | Row-level security enforced in PostgreSQL; all data is tenant-isolated |
| Auth | API key (Argon2id hash) + RS256 JWT exchange; fine-grained scopes |
| Rate limiting | Lua token-bucket per tenant; configurable capacity and refill rate |
| Observability | OTel traces → Tempo, Prometheus metrics, structlog JSON → Loki, Grafana dashboards (opt-in) |
| Admin frontend | Next.js 15 app at `frontend/` — upload, search, document detail, admin views |

Not built (do not expect): PPTX/email/archive handlers, billing or quota tiers, sentiment/classification, RAPTOR/GraphRAG, ColPali visual retrieval.

### LLM provider

Summarization and vision captioning work with any of four providers — pick the one that fits your setup:

| Provider | `LLM_PROVIDER` | Text default | Vision default | Key needed |
|---|---|---|---|---|
| Anthropic | `anthropic` | `claude-haiku-4-5-20251001` | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `gpt-4o-mini` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| Google | `google` | `gemini-2.0-flash` | `gemini-2.0-flash` | `GOOGLE_API_KEY` |
| Ollama (local) | `ollama` | `qwen2.5:7b` | `qwen2.5-vl:7b` | _(none)_ |

Override the model for either task with `LLM_TEXT_MODEL` / `LLM_VISION_MODEL`.

LLM enrichment (summaries and vision captions) is **off by default** even when a provider is configured. Enable it per-tenant by patching the tenant config:

```bash
curl -X PUT http://localhost:8000/v1/tenant/config \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"llm_enrichment_enabled": true}'
```

OCR and speech-to-text always run regardless of this flag or whether any LLM key is set.

**Zero-cost Ollama quickstart:**
```bash
docker run -d -p 11434:11434 ollama/ollama
ollama pull qwen2.5:7b        # text summarization
ollama pull qwen2.5-vl:7b     # image and video frame captioning
# then set LLM_PROVIDER=ollama in .env
```
Models load on first request and unload automatically when idle.

## Prerequisites

- Docker and Docker Compose
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 18+ (only for the optional frontend)
- NVIDIA Container Toolkit (optional — needed only for GPU-accelerated audio/video/OCR in Docker)

## Quickstart

### 1. Clone and copy env

```bash
git clone https://github.com/<your-org>/omnivore
cd omnivore
cp .env.example .env
```

### 2. Generate the JWT keypair

The RS256 keypair is required for API key auth. Generate it once and store both keys as a **single line of base64** in `.env`:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out private.pem
openssl rsa -pubout -in private.pem -out public.pem

# Append both keys as single-line base64 (docker-safe):
echo "JWT_PRIVATE_KEY_PEM=$(base64 -w0 private.pem)" >> .env
echo "JWT_PUBLIC_KEY_PEM=$(base64 -w0 public.pem)"   >> .env
# macOS: use `base64 -i private.pem` (no -w0 flag)
```

> **Why base64?** `docker compose`'s `.env` parser does not support multi-line values, so pasting a raw multi-line PEM breaks `docker compose up` with a parse error. A single line of base64 parses identically everywhere. The app normalizes base64, raw PEM, and `\n`-escaped forms to real PEM at load time, so a raw PEM still works when running outside docker (uv/uvicorn).
- Set `ADMIN_BOOTSTRAP_TOKEN` to a strong random string:
  ```bash
  openssl rand -hex 32
  ```

> Keep `private.pem` off disk once you've pasted it into `.env`. The `.env` file itself must never be committed — it is already in `.gitignore`.

### 3. Start infrastructure

```bash
docker compose up -d postgres redis minio
uv sync
uv run alembic upgrade head
```

### 4. Start API and worker

```bash
# Terminal 1 — API (with live reload)
uv run uvicorn omnivore.api.main:app --reload

# Terminal 2 — CPU worker (first start downloads BGE-base, ~440 MB)
uv run python -m omnivore.worker.main

# Terminal 3 (optional) — GPU worker, for audio/video/image OCR
uv run python -m omnivore.worker.gpu_main
```

### 5. Bootstrap your first tenant and API key

The admin endpoints are protected by `ADMIN_BOOTSTRAP_TOKEN`. Use them once to create a tenant and mint an API key, then use that key for all subsequent requests.

```bash
# Create a tenant — the response includes an initial API key
curl -X POST http://localhost:8000/v1/admin/tenants \
  -H "X-Admin-Token: $ADMIN_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"slug": "default", "display_name": "Default"}'
# Response includes: tenant_id, api_key (shown once — save it)

# Or create an additional API key for an existing tenant
curl -X POST http://localhost:8000/v1/admin/tenants/<tenant_id>/api-keys \
  -H "X-Admin-Token: $ADMIN_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-key", "scopes": ["documents:read","documents:write","search:read","entities:read","handlers:read"]}'
```

### 6. Upload a document

```bash
curl -X POST http://localhost:8000/v1/documents \
  -H "X-API-Key: $YOUR_API_KEY" \
  -F "file=@path/to/your.pdf"
# → {"success": true, "data": {"document_id": "...", "status": "queued", "poll_url": "..."}}

# Poll for completion
curl http://localhost:8000/v1/documents/<document_id> \
  -H "X-API-Key: $YOUR_API_KEY"
# → {"success": true, "data": {"status": "indexed", ...}}
```

### 7. Search

```bash
curl -X POST http://localhost:8000/v1/search \
  -H "X-API-Key: $YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "your question", "mode": "hybrid", "top_k": 5}'
```

### 8. Admin frontend (optional)

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000
```

The frontend reads `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`). Pages: `/documents` (upload, list, detail with summary/entities/routing/retry), `/search` (BM25/vector/hybrid toggle), `/admin` (handlers + readiness).

### 9. Monitoring stack (optional)

The observability stack (Tempo, Prometheus, Loki, Grafana) is opt-in via a Docker Compose profile.

```bash
# Generate a metrics auth token before starting
openssl rand -hex 32 > observability/auth_token
echo "METRICS_AUTH_TOKEN=$(cat observability/auth_token)" >> .env

docker compose --profile monitoring up -d
# Grafana: http://localhost:3001 (anonymous Admin access, no login required)
# Prometheus: http://localhost:9090
# Tempo: http://localhost:3200
# Note: Grafana runs on port 3001 to avoid conflict with the frontend dev server on 3000.
```

Three dashboards are auto-provisioned in Grafana: Pipeline Overview, Search Latency, and Tenant Activity.

## Service layout

| Component | Port | How to start |
|---|---|---|
| PostgreSQL (pgvector) | 5432 | `docker compose up -d postgres` |
| Redis | 6379 | `docker compose up -d redis` |
| MinIO (S3-compatible) | 9000 / 9001 console | `docker compose up -d minio` |
| FastAPI | 8000 | `uv run uvicorn omnivore.api.main:app --reload` |
| ARQ CPU worker | — | `uv run python -m omnivore.worker.main` |
| ARQ GPU worker | — | `uv run python -m omnivore.worker.gpu_main` |
| Next.js frontend | 3000 | `cd frontend && npm run dev` |
| Grafana | 3001 (monitoring profile) | `docker compose --profile monitoring up -d` |
| Prometheus | 9090 | (same) |
| Tempo | 4318 / 3200 | (same) |

Minimum required: Postgres, Redis, MinIO, the API, and the CPU worker. The GPU worker is needed only for audio/video/image OCR jobs.

## API reference

All responses use the `APIResponse[T]` envelope:
```json
{ "success": true, "data": <T>, "error": null, "meta": null }
```
Errors: `success: false`, `error: { code, message }`.

Document status values: `queued | routing | extracting | enriching | indexed | failed | duplicate`

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/health` | none | Liveness check |
| GET | `/v1/ready` | none | Readiness — checks Postgres, Redis, MinIO |
| GET | `/v1/handlers` | API key | List registered format handlers |
| POST | `/v1/documents` | API key | Upload a file — returns 202, 413, or 429 |
| GET | `/v1/documents` | API key | List documents (`?status=&limit=&cursor=`) |
| GET | `/v1/documents/{id}` | API key | Get document detail including summary and routing decision |
| GET | `/v1/documents/{id}/entities` | API key | Named entities extracted from the document |
| POST | `/v1/documents/{id}/retry` | API key | Re-queue a failed document |
| POST | `/v1/search` | API key | BM25 / vector / hybrid search |
| POST | `/v1/auth/token` | API key in body | Exchange an API key for a short-lived RS256 JWT |
| GET | `/v1/tenant` | JWT | Get current tenant info |
| GET | `/v1/tenant/config` | JWT | Get tenant routing config |
| PUT | `/v1/tenant/config` | JWT | Update tenant routing config |
| GET | `/v1/tenant/api-keys` | JWT | List own API keys |
| POST | `/v1/tenant/api-keys` | JWT | Create an additional API key |
| DELETE | `/v1/tenant/api-keys/{id}` | JWT | Revoke an API key |
| POST | `/v1/admin/tenants` | Admin token | Create a tenant (also mints an initial API key) |
| GET | `/v1/admin/tenants` | Admin token | List all tenants |
| PATCH | `/v1/admin/tenants/{id}` | Admin token | Update tenant metadata or config |
| POST | `/v1/admin/tenants/{id}/api-keys` | Admin token | Mint an API key for a tenant |
| DELETE | `/v1/admin/api-keys/{id}` | Admin token | Revoke any API key |

Full OpenAPI schema (with request/response shapes): `http://localhost:8000/docs`

## Configuration reference

All settings are read from `.env` via pydantic-settings. See `src/omnivore/config.py` for the full list.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://omnivore:omnivore@localhost:5432/omnivore` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO / S3 endpoint (no scheme) |
| `MINIO_ACCESS_KEY` | `omnivore` | MinIO access key |
| `MINIO_SECRET_KEY` | `omnivore123` | MinIO secret key |
| `MINIO_BUCKET` | `omnivore-raw` | Bucket for raw file blobs |
| `MINIO_SECURE` | `false` | Use TLS for MinIO connection |
| `JWT_PRIVATE_KEY_PEM` | _(empty)_ | RSA private key PEM — required for auth |
| `JWT_PUBLIC_KEY_PEM` | _(empty)_ | RSA public key PEM — required for auth |
| `JWT_ALGORITHM` | `RS256` | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_SECONDS` | `3600` | JWT TTL in seconds |
| `ADMIN_BOOTSTRAP_TOKEN` | `change-me-before-first-run` | Root admin token — change before first run |
| `RL_CAPACITY` | `100` | Rate limiter: max burst tokens per tenant |
| `RL_REFILL_RATE` | `10.0` | Rate limiter: tokens per second refill rate |
| `RL_UPLOAD_COST` | `10` | Tokens consumed per document upload |
| `RL_DEFAULT_COST` | `1` | Tokens consumed per other request |
| `LLM_PROVIDER` | `anthropic` | LLM backend: `anthropic` \| `openai` \| `google` \| `ollama` |
| `LLM_TEXT_MODEL` | _(provider default)_ | Override text model (empty = use provider default) |
| `LLM_VISION_MODEL` | _(provider default)_ | Override vision model (empty = use provider default) |
| `ANTHROPIC_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=openai` |
| `GOOGLE_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=google` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL (used when `LLM_PROVIDER=ollama`) |
| `VIDEO_FRAME_SAMPLE_INTERVAL` | `30` | Seconds between sampled video frames for vision captioning |
| `VIDEO_MAX_VISION_FRAMES` | `20` | Max frames captioned per video |
| `MAX_UPLOAD_SIZE_BYTES` | `2147483648` | Upload size limit (2 GB default) |
| `MAX_QUEUE_DEPTH` | `100` | CPU queue depth before HTTP 429 |
| `MAX_GPU_QUEUE_DEPTH` | `20` | GPU queue depth before HTTP 429 |
| `IMAGE_OCR_LANGUAGES` | `["en"]` | Default EasyOCR language list |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing (set `true` with `--profile monitoring`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP collector endpoint |
| `OTEL_SERVICE_NAME` | `omnivore` | Service name in traces |
| `METRICS_ENABLED` | `true` | Enable Prometheus metrics endpoint |
| `METRICS_AUTH_TOKEN` | _(empty)_ | Bearer token protecting `/metrics`; leave empty to disable auth |
| `LOG_LEVEL` | `INFO` | structlog log level |
| `ENVIRONMENT` | `development` | Environment name (`development` / `production`) |

## Development

```bash
# Install dependencies
uv sync

# Activate pre-commit hooks (runs ruff + unit tests on every commit)
uv run pre-commit install

# Start required infrastructure
docker compose up -d postgres redis minio
uv run alembic upgrade head

# Run the full test suite (needs infra running)
ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q

# Lint
uv run ruff check src/ tests/

# Run the eval harness (run this before shipping any handler change)
uv run python -m eval.harness --fixture-ids pdf-001 pdf-003
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor guide.

## Roadmap

- [x] **Phase 0 — Skeleton**: FastAPI, ARQ, PostgreSQL + pgvector schema, MinIO, Alembic, eval harness
- [x] **Phase 1 — Text and structured formats**: PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX handlers; structure-first chunker; document upload API
- [x] **Phase 2 — Embeddings and hybrid search**: BGE-base-en-v1.5 (768-dim, local); pgvector HNSW index; `POST /v1/search` with BM25, vector, and hybrid (RRF) modes
- [x] **Phase 2b — Heavy formats**: GPU worker queue; faster-whisper audio/video transcription; EasyOCR image OCR
- [x] **Phase 2c — Robustness**: `stream_blob()` for large files; multilingual OCR with LRU reader cache; backpressure (HTTP 429); idempotency guard; DLQ retry payload; transactional outbox
- [x] **Phase 3 — Enrichment**: Language detection (lingua); NER (spaCy); LLM summaries (Claude Haiku, optional); routing policy engine; `chunk_faithfulness` eval metric
- [x] **Phase 4 — Multi-tenant auth**: API keys (Argon2id); RS256 JWT exchange; fine-grained scopes; Lua token-bucket rate limiting; row-level security on all tables; admin and tenant self-service endpoints
- [x] **Phase 5 — Observability**: OTel traces → Tempo; Prometheus metrics; structlog JSON → Promtail → Loki; Grafana dashboards; W3C traceparent propagation across API → worker boundary
- [x] **Phase 6 — Hardening**: Chaos tests; DB partitioning (hash-partitioned chunks, range-partitioned jobs); cost dashboards; GUC pool-leak fix; open-source readiness (README, CI, CONTRIBUTING, DELETE endpoint, configurable CORS, partition management script)
- [x] **Phase 7 — Vision and provider flexibility**: Visual frame processing for images and video (semantic captions via pluggable LLM alongside OCR/STT); four-provider LLM layer (Anthropic / OpenAI / Google / Ollama — zero-cost local option); pre-commit hooks (ruff + unit tests); full unit test coverage for all 11 format handlers

**What's next**: PPTX/email/archive format handlers; eval fixtures for PDF/DOCX/XLSX/audio/video/image; RAPTOR/GraphRAG retrieval for multi-hop queries; Docling PDF backend (gated on eval win > 10%).

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full system design: pipeline stages, handler registry, queue topology, storage schema, and open architectural decisions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
