# omnidoc-ingest — Architecture

A universal document ingestion pipeline that accepts heterogeneous file inputs, extracts structured intelligence via format-specific handlers, enriches via LLM, and routes outputs to a relational store, a vector store, or both.

## Architectural Thesis

Three forces shape every decision below:

1. **Format heterogeneity** demands a registry/strategy pattern with hard isolation between handlers. A bad PDF parser must not poison the audio path.
2. **Compute asymmetry** — a 200-byte JSON file and a 4-hour MP4 enter the same endpoint. The architecture must absorb that variance without head-of-line blocking. This forces async-by-default with a tiered queue.
3. **Dual sink (relational + vector)** is not a switch flipped at the end — it's a routing decision that must be made per-extracted-fragment, not per-document, because one PDF may produce both structured rows (tables) and unstructured chunks (prose).

The design is opinionated: **Python 3.12 + FastAPI + ARQ + PostgreSQL with pgvector + MinIO**, containerized via Docker Compose for dev and Kubernetes for prod. Rationale follows.

---

## 1. High-level System Diagram

```
                                     ┌────────────────────────────┐
                                     │      Client / SDK          │
                                     │  (HTTP multipart, async)   │
                                     └──────────────┬─────────────┘
                                                    │
                                                    ▼
                          ┌──────────────────────────────────────────┐
                          │        FastAPI Gateway (stateless)       │
                          │  - Auth (JWT / API key)                  │
                          │  - Rate limit                            │
                          │  - Multipart streaming → object store    │
                          │  - Format sniffing (libmagic + ext)      │
                          │  - Enqueue ingest job                    │
                          └─────────────┬───────────────┬────────────┘
                                        │               │
                  (binary blob)         │               │ (job descriptor)
                                        ▼               ▼
                          ┌─────────────────────┐  ┌──────────────────┐
                          │  Object Store       │  │   Redis (ARQ)    │
                          │  MinIO / S3         │  │   - default Q    │
                          │  raw/{tenant}/{id}  │  │   - cpu Q        │
                          └──────────┬──────────┘  │   - gpu Q        │
                                     │             │   - io Q         │
                                     │             └────────┬─────────┘
                                     │                      │
                                     │     ┌────────────────┴───────────────┐
                                     │     │                                │
                                     ▼     ▼                                ▼
                          ┌────────────────────────┐         ┌────────────────────────┐
                          │   Worker Pool (ARQ)    │         │  GPU Worker Pool (ARQ) │
                          │   - extractors         │         │  - whisper.cpp / faster│
                          │   - enrichers          │         │  - local LLM (vLLM)    │
                          │   - chunker            │         │  - vision models       │
                          │   - embedder (API)     │         │  - embedder (local)    │
                          └───────────┬────────────┘         └────────────┬───────────┘
                                      │                                   │
                                      │     ┌─────────────────────────────┘
                                      ▼     ▼
                              ┌──────────────────────┐
                              │   Router Stage       │
                              │   policy engine      │
                              │   per-fragment sink  │
                              └────┬─────────────┬───┘
                                   │             │
                  (structured rows)│             │ (chunks + vectors)
                                   ▼             ▼
                       ┌──────────────────────────────────────┐
                       │     PostgreSQL 16 + pgvector         │
                       │  documents | chunks | entities |     │
                       │  tables    | events  | jobs          │
                       └──────────────────────────────────────┘
                                          ▲
                                          │
                                          │  RAG / metadata queries
                          ┌───────────────┴───────────────┐
                          │     Query API (FastAPI)       │
                          │   /search (hybrid: BM25+vec)  │
                          │   /documents/{id}             │
                          │   /entities/...               │
                          └───────────────────────────────┘

Cross-cutting:
  - OpenTelemetry (traces) → Tempo / Jaeger
  - Prometheus metrics, Grafana dashboards
  - structlog → Loki
  - Sentry for exceptions
```

---

## 2. Core Pipeline Stages

The pipeline is a **stage graph**, not a linear chain. Each stage is a pure function over an immutable `IngestContext` envelope. Stages emit new contexts; they never mutate input.

### 2.1 Ingestion

**Responsibility**: accept bytes, persist them, decide what they are, hand off to async processing.

- **Streaming upload**: FastAPI receives multipart, streams to object store via `aioboto3`/`miniopy-async` without buffering full file in memory. Cap at configurable size (default 2 GiB; reject larger upfront).
- **Validation**:
  - MIME sniffing via `python-magic` (libmagic), cross-checked with extension and declared `Content-Type`. Disagreement → reject with 415.
  - Virus scan hook (ClamAV sidecar, optional, per-tenant policy).
  - Hash on the fly (SHA-256, streaming) for dedup.
- **Dedup**: if hash matches existing document for tenant, return the prior `document_id` with `status=duplicate`. Saves compute on re-uploads.
- **Persistence**: blob → `s3://omnidoc-raw/{tenant_id}/{yyyy}/{mm}/{document_id}.{ext}`. Row inserted in `documents` with `status='queued'`.
- **Enqueue**: emit `ingest.dispatch` job with `{document_id, mime, size, tenant_id, config_snapshot}`. Return 202 with polling URL.

### 2.2 Extraction

**Responsibility**: turn bytes into a normalized intermediate representation (NIR).

NIR is a typed Pydantic model:

```python
class ExtractionResult:
    document_id: UUID
    fragments: list[Fragment]       # ordered, addressable units
    tables: list[StructuredTable]
    metadata: dict[str, Any]        # EXIF, codec info, page count, etc.
    warnings: list[str]
    source_handler: str
    handler_version: str

class Fragment:
    fragment_id: UUID
    kind: Literal['text','caption','transcript','ocr','code','table_row']
    content: str
    position: PositionRef           # page+bbox, time range, row index, etc.
    confidence: float | None
    language: str | None
```

**Handlers** (see registry §3):
- **PDF**: PyMuPDF for layout-aware text + per-page raster fallback to OCR (Tesseract or PaddleOCR) when text density < threshold. Tables via `pdfplumber` or Camelot.
- **DOCX/PPTX/XLSX**: `python-docx`, `python-pptx`, `openpyxl`. Sheets become `StructuredTable`.
- **Images**: Pillow for EXIF + OCR + optional vision-LLM caption.
- **Audio**: ffmpeg normalize → 16kHz mono → `faster-whisper` (CTranslate2) on GPU worker. Outputs transcript with word timestamps.
- **Video**: ffmpeg → demux audio (→ audio path), keyframe extraction every N seconds → image path for OCR/captions. Both merge by timestamp into a single `ExtractionResult`.
- **CSV/TSV**: Polars (Arrow-backed, lazy) → `StructuredTable` + per-row `Fragment` if row-level RAG is enabled.
- **JSON/YAML/XML**: jsonpath-based flattening to either tables or fragments depending on shape (array-of-objects → table, free-form → fragments).
- **HTML/Markdown**: `selectolax` / `markdown-it-py` with semantic block boundaries preserved.
- **Email (.eml/.msg)**: `mailparser`/`extract-msg`, attachments recursively re-enter the pipeline as child documents.
- **Archives (.zip/.tar)**: explode, each member becomes a child document linked via `parent_document_id`.

Extraction is **idempotent**: re-running on the same blob produces equivalent NIR (modulo handler version). Handler version is recorded so re-extraction on upgrade is selectable.

### 2.3 Enrichment

**Responsibility**: add semantic layers on top of NIR.

Enrichment is a fan-out stage with parallel sub-tasks per fragment / per document:

- **Chunking**: hierarchical — section-aware splitter (markdown headers, PDF sections, transcript speaker turns) then token-aware sliding window (default 512 tokens, 64 overlap). `semchunk` for token-accurate splits with `tiktoken`.
- **Embedding**: configurable model (default `text-embedding-3-small`, 1536-dim, via OpenAI; local fallback `bge-small-en-v1.5` 384-dim via `sentence-transformers` on GPU worker). Batched (64 chunks/call). Cached by `sha256(text + model_id)` to avoid re-embedding.
- **Summary**: per-document and per-section, via Claude/GPT-4o-mini. Structured output (Pydantic schema) with title, abstract, key_points, topics.
- **NER**: spaCy (`en_core_web_trf`) for offline baseline + LLM-assisted extraction for domain entities driven by tenant-supplied schema.
- **Sentiment / classification**: optional, per-tenant config. Off by default.
- **Language detection**: `lingua-py` (more accurate than fastText for short text).
- **PII redaction**: optional — `presidio` for detection, deterministic tokenization for storage.

Enrichment outputs are appended to the context envelope; nothing is overwritten.

### 2.4 Routing

**Responsibility**: decide per-fragment and per-table which sinks receive it.

A pure-function policy engine evaluates a tenant config:

```yaml
default_sinks: ['relational', 'vector']
rules:
  - match: { kind: 'table_row' }
    sinks: ['relational']
  - match: { kind: 'transcript', confidence: '<0.6' }
    sinks: []                   # drop low-quality
  - match: { mime: 'application/json', shape: 'array_of_objects' }
    sinks: ['relational']
  - match: { kind: 'text' }
    sinks: ['vector']
```

Rules are declarative (YAML/JSON), versioned, and validated on tenant update. The engine is a stateless function `(fragment, doc_meta, policy) → set[Sink]`. Each fragment carries the matched rule id in its audit row.

### 2.5 Storage

**Responsibility**: durable writes with transactional consistency between relational rows and vector rows where they refer to the same fragment.

Single PostgreSQL instance with `pgvector` handles both. One transaction per document finalization writes:
1. Update `documents.status = 'indexed'`
2. Insert all `chunks` (with embeddings)
3. Insert all `entities`
4. Insert structured-table rows (dynamic per-tenant schemas, see §5)
5. Emit `document.indexed` event to outbox table

The outbox pattern decouples downstream notifications (webhooks, search index sync) from the write transaction.

---

## 3. Format Handler Registry

A handler is any class implementing:

```python
class FormatHandler(Protocol):
    name: ClassVar[str]                          # 'pdf', 'whisper-audio'
    version: ClassVar[str]                       # semver of handler
    accepts: ClassVar[tuple[MimeMatcher, ...]]   # mime patterns + magic bytes
    cost_class: ClassVar[Literal['io','cpu','gpu']]
    timeout_seconds: ClassVar[int]

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult: ...
```

### Registration

Handlers self-register via Python entry points (`pyproject.toml`):

```toml
[project.entry-points."omnidoc.handlers"]
pdf = "omnidoc_handlers.pdf:PdfHandler"
audio_whisper = "omnidoc_handlers.audio:WhisperHandler"
```

At process start, the registry walks entry points, instantiates handlers, and indexes them by `(mime_pattern, magic_signature)`. Third-party packages drop a wheel into the image and become available — zero core changes.

### Dispatch

```
HandlerRegistry.resolve(detected_mime, magic_bytes) → FormatHandler
```

Resolution is most-specific-first (e.g., `application/pdf` beats `application/octet-stream`). Tie-breaks via explicit `priority` field.

### Queue Routing by Cost Class

Handler's `cost_class` determines which ARQ queue receives the job:
- `io` → `q:io` (small worker pool, high concurrency — JSON, CSV under 10 MB)
- `cpu` → `q:cpu` (PDF, OCR, DOCX)
- `gpu` → `q:gpu` (Whisper, vision models, local LLM)

This is the single most important scalability lever — heavy jobs cannot starve light ones.

---

## 4. Async / Job Queue Design

### Choice: ARQ (asyncio Redis Queue)

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **ARQ** | Native asyncio, lightweight, Redis-only, simple API | Smaller community than Celery | **Chosen** |
| Celery | Battle-tested, huge ecosystem | Sync-first, awkward asyncio | Overkill for greenfield async stack |
| Dramatiq | Clean API | Sync-first | Close runner-up |
| Temporal | Best-in-class durability, workflow graph | Heavy infra, own server | Re-evaluate at scale |

**Decision: ARQ now, abstract the interface cleanly so we can swap to Temporal at multi-region scale.**

### Queue Topology

```
q:io        concurrency: 64/worker, 2-4 workers   — JSON, CSV<10MB, text/markdown
q:cpu       concurrency: 4/worker, N≈cores×0.75   — PDF, OCR, DOCX, images
q:gpu       concurrency: 1-2/worker, 1 per GPU    — Whisper, vision, local LLM
q:enrich    concurrency: 32/worker                — embedding, summary, NER
q:write     concurrency: 16/worker                — final DB writes, serialized per doc
```

### Job Graph (DAG per document)

```
ingest.dispatch
    │
    ▼
extract.run  ──► (per fragment) ──► enrich.embed ─┐
    │                              enrich.ner    │
    │                              enrich.summary┤
    │                                            ▼
    └──────────────────────────────────► route.and_write
                                                 │
                                                 ▼
                                         document.finalize
                                         (emit outbox event)
```

Each edge is an enqueue. A failed `enrich.embed` retries with exponential backoff (max 5, jitter) without re-running `extract.run`.

### Backpressure & Idempotency

- Per-tenant in-flight cap (100 jobs default) enforced via Redis `INCR`/`DECR`; over-cap → 429.
- Every job carries `idempotency_key = sha256(stage + document_id + handler_version + config_hash)`. Workers `SET NX` before running; duplicates are no-ops.
- Dead-letter queue (`q:dlq`) for jobs exceeding retry budget; ops dashboard shows DLQ depth + last error.

---

## 5. Storage Schema

PostgreSQL 16 + `pgvector` 0.7+ (HNSW indexes).

```sql
-- Tenants
CREATE TABLE core.tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT UNIQUE NOT NULL,
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per upload
CREATE TABLE core.documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES core.tenants(id),
    parent_document_id  UUID REFERENCES core.documents(id),
    sha256              BYTEA NOT NULL,
    filename            TEXT NOT NULL,
    mime_type           TEXT NOT NULL,
    size_bytes          BIGINT NOT NULL,
    storage_uri         TEXT NOT NULL,
    handler_name        TEXT,
    handler_version     TEXT,
    status              TEXT NOT NULL CHECK (status IN (
                            'queued','extracting','enriching','indexed','failed','duplicate')),
    error               JSONB,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    routing_decision    JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at          TIMESTAMPTZ,
    UNIQUE (tenant_id, sha256)
);

CREATE INDEX idx_documents_tenant_status ON core.documents(tenant_id, status);
CREATE INDEX idx_documents_metadata ON core.documents USING GIN(metadata jsonb_path_ops);
CREATE INDEX idx_documents_created ON core.documents(tenant_id, created_at DESC);

-- Chunks / fragments for RAG
CREATE TABLE core.chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES core.tenants(id),
    ordinal         INT NOT NULL,
    kind            TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_tsv     TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    token_count     INT NOT NULL,
    position        JSONB NOT NULL,
    embedding       vector(1536),
    embedding_model TEXT,
    language        TEXT,
    confidence      REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW index (partial — skips chunks with no embedding)
CREATE INDEX idx_chunks_embedding ON core.chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE embedding IS NOT NULL;

CREATE INDEX idx_chunks_tsv ON core.chunks USING GIN(content_tsv);
CREATE INDEX idx_chunks_doc ON core.chunks(document_id, ordinal);

-- Named entities
CREATE TABLE core.entities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES core.tenants(id),
    chunk_id    UUID REFERENCES core.chunks(id) ON DELETE SET NULL,
    label       TEXT NOT NULL,
    value       TEXT NOT NULL,
    normalized  TEXT,
    confidence  REAL,
    metadata    JSONB DEFAULT '{}'::jsonb,
    UNIQUE (document_id, label, normalized)
);

-- Extracted structured tables
CREATE TABLE core.extracted_tables (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES core.tenants(id),
    name        TEXT NOT NULL,
    schema      JSONB NOT NULL,
    row_count   INT NOT NULL
);

CREATE TABLE core.extracted_rows (
    table_id UUID NOT NULL REFERENCES core.extracted_tables(id) ON DELETE CASCADE,
    ordinal  INT NOT NULL,
    data     JSONB NOT NULL,
    PRIMARY KEY (table_id, ordinal)
);

CREATE INDEX idx_rows_data ON core.extracted_rows USING GIN(data jsonb_path_ops);

-- Job audit log (queue is ephemeral in Redis; this is durable)
CREATE TABLE core.jobs (
    id          UUID PRIMARY KEY,
    document_id UUID REFERENCES core.documents(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    error       JSONB,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    duration_ms INT
);

-- Transactional outbox for downstream events
CREATE TABLE core.outbox (
    id           BIGSERIAL PRIMARY KEY,
    aggregate_id UUID NOT NULL,
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX idx_outbox_unpublished ON core.outbox(id) WHERE published_at IS NULL;
```

### Vector Store Choice: pgvector

| Option | Pros | Cons |
|--------|------|------|
| **pgvector** | Single datastore, transactional with relational data, hybrid search trivial | Slower than dedicated stores at >10M vectors per index |
| Qdrant | Excellent perf, payload filters | Second datastore, sync complexity |
| Weaviate | Schema, modules, hybrid built-in | Heavier, opinionated |
| Milvus | Best at huge scale (100M+) | Significant infra; overkill below 10M vectors |

**Decision: pgvector for v1.** Transactional consistency between metadata and vectors is worth more than the 2-3x latency win from a dedicated store at the scale we'll hit in year one. Abstract the read path behind a `VectorIndex` interface so swapping to Qdrant later is bounded work.

---

## 6. API Surface

REST + JSON. OpenAPI spec auto-generated by FastAPI. All responses use:

```json
{ "success": bool, "data": {...} | null, "error": {...} | null, "meta": {...} | null }
```

### Upload

```
POST /v1/documents
  Content-Type: multipart/form-data
  Body: file (binary) + config (JSON, optional)
  → 202 { "document_id": "...", "status": "queued", "poll_url": "/v1/documents/{id}" }

POST /v1/documents/batch          — multiple files
POST /v1/documents/url            — fetch from URL asynchronously
POST /v1/documents/{id}/reprocess — re-run selected stages
```

### Status / Polling

```
GET  /v1/documents/{id}           — status + per-stage progress + metadata
GET  /v1/documents/{id}/events    — SSE stream of stage transitions
GET  /v1/documents?status=failed&since=...&limit=...&cursor=...
```

### Query

```
POST /v1/search
  Body: {
    "query": "natural language",
    "mode": "hybrid",              // vector | bm25 | hybrid
    "filters": {
      "document.mime_type": "application/pdf",
      "chunk.kind": ["text","transcript"],
      "created_after": "2026-01-01"
    },
    "top_k": 20,
    "rerank": true                 // cross-encoder rerank
  }

GET /v1/documents/{id}/chunks?limit=&cursor=
GET /v1/documents/{id}/entities?label=PERSON
GET /v1/tables/{table_id}/rows?filter=...
```

Hybrid search: parallel `tsvector` + vector queries with Reciprocal Rank Fusion (RRF, k=60). Optional cross-encoder rerank (`bge-reranker-base`) on top-50 → top-K.

### Admin

```
GET  /v1/health
GET  /v1/ready                    — checks Postgres + Redis + S3
GET  /v1/handlers                 — list registered handlers + versions
PUT  /v1/tenants/{id}/policy      — update routing policy
GET  /v1/jobs/dlq
POST /v1/jobs/dlq/{job_id}/retry
```

### Auth

- API key per tenant (header `X-API-Key`) for service-to-service.
- JWT (RS256) for human users via OAuth/OIDC.
- Rate limit: token bucket per-tenant in Redis, headers `X-RateLimit-*`.

---

## 7. Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12 | Best ML/extraction ecosystem; async mature; Pydantic v2 |
| Web framework | FastAPI 0.110+ | ASGI-native, Pydantic v2, OpenAPI free |
| ASGI server | Granian (prod) / Uvicorn (dev) | Granian (Rust) ~30% throughput gain on streaming uploads |
| Job queue | ARQ + Redis 7 | Native asyncio, lightweight, see §4 |
| Object storage | MinIO (dev/self-host) / S3 (cloud) | S3 API standard, swap by config |
| Database | PostgreSQL 16 + pgvector 0.7 | See §5 |
| Migrations | Alembic | Standard, async-compatible |
| DB driver | asyncpg via SQLAlchemy 2.0 async | Portability + speed |
| Embedding (default) | OpenAI `text-embedding-3-small` | Best $/quality, 1536-dim, batched |
| Embedding (local) | `bge-small-en-v1.5` via Infinity | Air-gapped tenants |
| LLM (default) | Claude Haiku (enrichment) / Sonnet (summary) | Strong structured output, low latency |
| LLM (local) | vLLM + Llama-3.1-8B | Self-hosted tenants |
| ASR | `faster-whisper` (large-v3 GPU / distil CPU) | 4-6x faster than reference Whisper |
| OCR | PaddleOCR primary, Tesseract fallback | Paddle more accurate on mixed-language/tabular |
| PDF | PyMuPDF + pdfplumber | Note: PyMuPDF is AGPL — see §9 |
| NER | spaCy `en_core_web_trf` + LLM-assisted | Hybrid keeps cost low |
| Chunking | `semchunk` + section-aware splitter | Token-accurate, structure-preserving |
| Reranker | `bge-reranker-base` via Infinity | Cheap quality boost |
| Cache | Redis (separate logical DB) | Embeddings cache, dedup, rate limit |
| Observability | OTel + Tempo + Prometheus + Grafana + Loki + Sentry | OTel is the lingua franca |
| Logging | structlog → JSON → Loki | Structured logs are non-negotiable in async systems |
| Containers | Docker multi-stage + Compose (dev) + Kubernetes (prod) | Separate images per worker type |
| Package manager | `uv` | 10-100x faster than pip, lockfile, drop-in |
| Testing | pytest + pytest-asyncio + testcontainers | Real dependencies in tests beat mocks |

### Container Images

```
images/
  api/           — FastAPI gateway
  worker-io/     — ARQ worker, q:io
  worker-cpu/    — ARQ worker, q:cpu (PDF, OCR, ffmpeg, Tesseract)
  worker-gpu/    — ARQ worker, q:gpu (CUDA base, faster-whisper, vLLM optional)
  worker-enrich/ — ARQ worker, q:enrich
```

---

## 8. Scalability Considerations

### Single-user / dev
Docker Compose: api + 1 all-queues worker + Postgres + Redis + MinIO. Works to ~100 docs/hour.

### Small SaaS (10-50 tenants, ~10K docs/day)
- API replicas behind LB (3+)
- Worker pools split by queue: 4 cpu, 2 gpu, 4 io, 2 enrich
- Postgres: primary + read replica; chunks/entities reads → replica
- Redis: single instance with persistence + replica

### Mid-scale (100s tenants, 100K-1M docs/day)

Bottlenecks in order of arrival:

1. **GPU worker capacity** — video/audio. Mitigation: autoscaling GPU node pool, spot instances, batch Whisper jobs.
2. **Embedding API cost** — Mitigation: aggressive cache, batch sizing, self-hosted Infinity for large tenants.
3. **Postgres write throughput on `chunks`** — Mitigation: declarative partitioning by `tenant_id` hash (16-32 partitions), `synchronous_commit = off` for chunk inserts, bulk `COPY` from worker.
4. **HNSW index build time during bulk loads** — Mitigation: start with `ef_construction=64`, raise after warm-up; IVFFlat for cold partitions.
5. **Object store egress on reprocessing** — Mitigation: cache NIR alongside raw (`processed/{id}.json.zst`) so re-enrichment skips re-extraction.

### Large scale (multi-region, >10M docs/day)
- Move vector search to Qdrant (cluster, sharded); sync via outbox → Kafka → Qdrant consumer
- Replace ARQ with Temporal for orchestration
- Multi-region: regional ingest endpoints + object stores, async metadata replication (Citus / CockroachDB)
- Per-tenant dedicated worker pools for top tenants (noisy-neighbor mitigation)

### Hard limits
- Single document: 2 GiB default
- Max processing time per document: 6 hours
- Per-tenant in-flight cap: 100 concurrent documents default

---

## 9. Open Questions / Decisions to Make

| # | Question | My pick |
|---|----------|---------|
| 1 | **LLM hosting**: API (Anthropic) vs fully air-gapped (vLLM)? | API for v1, vLLM track planned for v2 |
| 2 | **Storage hosting**: cloud (S3/RDS) vs on-prem (MinIO/self-managed PG)? | Cloud-first, config-driven so on-prem is a deploy variant |
| 3 | **Multi-tenancy model**: shared DB / schema-per-tenant / DB-per-tenant? | Shared DB + RLS for v1; schema-per-tenant for top 1% if noisy-neighbor becomes real |
| 4 | **PyMuPDF AGPL licensing**: if distributed on-prem to customers, AGPL obligations attach | Confirm distribution model; fallback to `pypdf` + `pdfminer.six` (BSD/MIT) if uncertain |
| 5 | **Vision/captioning**: local BLIP-2 vs LLM API (Claude vision / GPT-4o)? | LLM API — simpler, better quality |
| 6 | **Routing policy DSL**: simple match rules vs full jsonlogic/CEL? | jsonlogic — avoids custom DSL, security-bounded |
| 7 | **PII handling**: detect-and-flag vs detect-and-redact-at-rest? | Configurable per-tenant; default flag (less destructive), redact opt-in |
| 8 | **Chunk size defaults**: one-size-fits-all vs per-handler presets? | Per-handler defaults overridable per-tenant; ship named presets |
| 9 | **Evaluation harness**: when to build? | Before the second handler version — this is the #1 thing teams skip and regret |
| 10 | **Auth source of truth**: build internal vs Keycloak / Clerk? | Keycloak (self-hosted on-prem), Clerk (cloud-only) |
| 11 | **ARQ → Temporal migration trigger**: when? | When DLQ/retry observability becomes painful, or workflow durability > 24h needed |
| 12 | **Cost caps**: per-tenant budget for LLM/embedding calls? | Instrument cost metrics in v1; enforce caps in v1.1 |

---

## 10. Implementation Phasing

| Phase | Scope | Duration |
|-------|-------|----------|
| **0 — Skeleton** | Repo scaffold (uv, ruff, pyright, pytest), Docker Compose, FastAPI, Postgres + Alembic, Redis, MinIO, ARQ no-op worker, CI green | 1 week |
| **1 — Text formats** | PDF, DOCX, TXT, MD, HTML, JSON, CSV handlers; chunking; embeddings (OpenAI); pgvector; hybrid search; polling endpoint | 2 weeks |
| **2 — Heavy formats** | GPU worker + `faster-whisper`, video pipeline (ffmpeg), image OCR (PaddleOCR), backpressure, idempotency, DLQ, SSE progress | 2 weeks |
| **3 — Enrichment** | LLM summarization, NER, language detection, routing policy engine + jsonlogic | 1 week |
| **4 — Multi-tenant + auth** | Tenants, API keys, JWT, rate limit, RLS policies, per-tenant config | 1 week |
| **5 — Observability + eval** | OTel, Prometheus, Grafana, Loki, eval harness with golden set | 1 week |
| **6 — Hardening** | Chaos tests on workers, DB partition rollout, cost dashboards, compliance groundwork | Ongoing |
