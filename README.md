# Omnivore

A Postgres-native document ingestion pipeline for RAG systems. Upload text documents and structured data — PDF, DOCX, spreadsheets, HTML, JSON — and get back structured intelligence stored directly in PostgreSQL: chunked text with full lineage, extracted tables queryable as SQL, and rich metadata. No separate vector database required.

> **Current state (Phase 2b Done — Phase 2c next):** Phase 1 text + structured-data formats work end-to-end. Phase 2 (local BGE-base embeddings + hybrid BM25/vector/RRF search) shipped 2026-05-07. Phase 2b (audio via faster-whisper, video via ffmpeg, image OCR via EasyOCR, GPU worker queue, embedding bakeoff) shipped 2026-05-07 — 176 unit tests, 6 E2E tests, `ruff` clean. Bakeoff: **BGE-M3 +18.7% recall@1 vs BGE-base** on a pooled 9-chunk corpus. Architect audits closed for both. See [`docs/phase2-audit.md`](docs/phase2-audit.md), [`docs/phase2b-audit.md`](docs/phase2b-audit.md).

## What works today

```
You upload a file (PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX)
        │
        ▼
Omnivore detects the format, routes it to the right handler,
extracts text blocks and tables, chunks content structure-first,
and stores everything in PostgreSQL.
        │
        ▼
You query chunks and tables — SQL JOINs across document text
and extracted spreadsheet rows from one database.
```

## Supported formats

| Category | Formats | Status |
|---|---|---|
| Documents | PDF, DOCX, TXT, MD, HTML | ✅ Phase 1 |
| Structured data | JSON, CSV, TSV, XLSX | ✅ Phase 1 |
| Audio | MP3, WAV, M4A, FLAC, OGG, WebM | ✅ Phase 2b |
| Video | MP4, MOV, MKV, AVI, WEBM, OGV | ✅ Phase 2b |
| Images | JPEG, PNG, WEBP, TIFF, BMP, GIF | ✅ Phase 2b |
| Office (presentations) | PPTX | 🗓 Phase 3+ |
| Email | .eml, .msg | 🗓 Phase 3+ |
| Archives | .zip, .tar | 🗓 Phase 3+ |

## Extracted intelligence

| Signal | Description | Status |
|---|---|---|
| Text chunks + embeddings | Structure-first chunking + 768-dim BGE-base-en-v1.5 vectors in pgvector | ✅ Phase 2 |
| Hybrid search | BM25 + vector + RRF merge over `/v1/search` | ✅ Phase 2 |
| Structured tables | Rows from CSV, XLSX, PDF tables, JSON arrays | ✅ Phase 1 |
| Named entities | People, orgs, locations, dates | 🗓 Phase 3 |
| Document summary | LLM-generated abstractive summary | 🗓 Phase 3 |
| Sentiment / classification | Per-document and per-section scores | 🗓 Phase 3 |
| Transcription | Audio/video speech-to-text via faster-whisper (`base` model, GPU/CPU) | ✅ Phase 2b |
| OCR | Image text extraction via EasyOCR (English) | ✅ Phase 2b |
| EXIF / codec metadata | File-level technical metadata | ✅ Phase 1 |

## Implementation roadmap

- [x] **Phase 0 — Skeleton**: FastAPI gateway, ARQ worker, PostgreSQL schema with pgvector, MinIO object store, Docker Compose, Alembic migrations, eval harness skeleton
- [x] **Phase 1 — Text & structured formats**: PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX handlers · structure-first chunker · document upload API · full pipeline loop (upload → extract → chunk → index)
- [x] **Phase 2 — Embeddings + hybrid search**: local BGE-base-en-v1.5 (768-dim) via sentence-transformers · pgvector HNSW index · `POST /v1/search` with BM25, vector, and RRF hybrid modes · Redis cache for query embeddings · 126 unit tests · 6 E2E tests · 5/5 eval fixtures · `ruff` clean — *shipped, architect audit closed 2026-05-07 (see [`docs/phase2-audit.md`](docs/phase2-audit.md))*
- [x] **Phase 2b — Heavy formats**: GPU worker queue (`arq:gpu`, cost-class routing) · faster-whisper (audio: MP3/WAV/M4A/FLAC/OGG/WebM) · ffmpeg + faster-whisper video pipeline · EasyOCR (images: JPEG/PNG/WEBP/TIFF/BMP/GIF) · embedding bakeoff with pooled-corpus methodology (BGE-base vs BGE-M3 vs nomic-embed) — *shipped, BGE-M3 +18.7% recall@1; switching gated on larger corpus + DB migration. Architect audit closed 2026-05-07 (see [`docs/phase2b-audit.md`](docs/phase2b-audit.md))*
- [ ] **Phase 2c — Robustness & multilingual**: blob streaming for >500 MB media · multilingual OCR · larger eval corpus (50+ chunks) · backpressure + idempotency + DLQ · automated regression tests for size guards
- [ ] **Phase 3 — Enrichment**: LLM summarization · NER (spaCy + LLM-assisted) · routing policy engine (jsonlogic) · RAGChecker / ARES eval metrics wired in
- [ ] **Phase 4 — Multi-tenant + auth**: API keys · JWT (RS256) · rate limiting · row-level security · per-tenant routing policies
- [ ] **Phase 5 — Observability**: OpenTelemetry traces · Prometheus metrics · Grafana dashboards · Loki structured logs · Sentry exceptions
- [ ] **Phase 6 — Hardening**: Chaos tests · DB partitioning (chunks by tenant) · cost dashboards · compliance groundwork

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for full system design: pipeline stages, handler registry, queue topology, storage schema, API surface, scalability considerations, and open decisions.

## Getting started

**Requirements**: Docker, Python 3.12+, `uv`

```bash
git clone https://github.com/hazarsozer/project-omnivore
cd project-omnivore

uv sync
cp .env.example .env

docker compose up -d postgres redis minio
uv run alembic upgrade head

# API (with live reload)
uv run uvicorn omnivore.api.main:app --reload

# Worker (separate terminal — first start downloads BGE-base ~440 MB)
uv run python -m omnivore.worker.main
```

**Upload a file:**
```bash
curl -X POST http://localhost:8000/v1/documents \
     -F "file=@your-document.pdf"
# → {"success": true, "data": {"document_id": "...", "status": "queued", "poll_url": "/v1/documents/..."}}

curl http://localhost:8000/v1/documents/{document_id}
# → {"success": true, "data": {"status": "indexed", "chunks": 42, ...}}
```

**Search (Phase 2):**
```bash
curl -X POST http://localhost:8000/v1/search \
     -H "Content-Type: application/json" \
     -d '{"query": "document ingestion pipeline", "mode": "hybrid", "top_k": 5}'
# → {"success": true, "data": [{"chunk_id": "...", "content": "...", "score": 0.0312, ...}, ...]}
```

**Explore the API:** `http://localhost:8000/docs`

## Contributing

See [`CLAUDE.md`](CLAUDE.md) for architecture decisions, coding conventions, and what's in scope for each phase. If you're using Claude Code, it will load this automatically.

## License

MIT
