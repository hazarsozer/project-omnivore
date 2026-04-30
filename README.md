# Omnivore

A Postgres-native document ingestion pipeline for RAG systems. Upload text documents and structured data — PDF, DOCX, spreadsheets, HTML, JSON — and get back structured intelligence stored directly in PostgreSQL: chunked text with full lineage, extracted tables queryable as SQL, and rich metadata. No separate vector database required.

> **Current state (Phase 1 complete):** Text and structured-data formats work end-to-end — upload → extract → chunk → persist. Audio, video, OCR, embeddings, and search are planned for Phase 2+. See the roadmap below.

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
| Audio | MP3, WAV, M4A, FLAC, OGG | 🔧 Phase 2 |
| Video | MP4, MOV, MKV, AVI, WEBM | 🔧 Phase 2 |
| Images | JPEG, PNG, WEBP, TIFF | 🔧 Phase 2 |
| Office (presentations) | PPTX | 🗓 Phase 3+ |
| Email | .eml, .msg | 🗓 Phase 3+ |
| Archives | .zip, .tar | 🗓 Phase 3+ |

## Extracted intelligence

| Signal | Description | Status |
|---|---|---|
| Text chunks + embeddings | Semantically chunked content with dense vectors for RAG | 🔧 Phase 2 (embedding bakeoff) |
| Structured tables | Rows from CSV, XLSX, PDF tables, JSON arrays | ✅ Phase 1 |
| Named entities | People, orgs, locations, dates | 🗓 Phase 3 |
| Document summary | LLM-generated abstractive summary | 🗓 Phase 3 |
| Sentiment / classification | Per-document and per-section scores | 🗓 Phase 3 |
| Transcription | Audio/video speech-to-text via faster-whisper | 🔧 Phase 2 |
| OCR | Scanned PDF and image text extraction | 🔧 Phase 2 |
| EXIF / codec metadata | File-level technical metadata | ✅ Phase 1 |

## Implementation roadmap

- [x] **Phase 0 — Skeleton**: FastAPI gateway, ARQ worker, PostgreSQL schema with pgvector, MinIO object store, Docker Compose, Alembic migrations, eval harness skeleton
- [x] **Phase 1 — Text & structured formats**: PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX handlers · structure-first chunker · document upload API · full pipeline loop (upload → extract → chunk → index)
- [ ] **Phase 2 — Heavy formats + embedding bakeoff**: GPU worker · faster-whisper (audio) · ffmpeg video pipeline · PaddleOCR (images) · embedding model bakeoff (`text-embedding-3-small` vs BGE-M3 vs jina-v3) · backpressure + idempotency + DLQ
- [ ] **Phase 3 — Enrichment + search**: LLM summarization · NER (spaCy + LLM-assisted) · routing policy engine (jsonlogic) · hybrid search (BM25 + vector + RRF) · RAGChecker / ARES eval metrics wired in
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
alembic upgrade head

# API (with live reload)
uvicorn omnivore.api.main:app --reload

# Worker (separate terminal)
python -m omnivore.worker.main
```

**Upload a file:**
```bash
curl -X POST http://localhost:8000/v1/documents \
     -F "file=@your-document.pdf"
# → {"success": true, "data": {"document_id": "...", "status": "queued", "poll_url": "/v1/documents/..."}}

curl http://localhost:8000/v1/documents/{document_id}
# → {"success": true, "data": {"status": "indexed", "chunks": 42, ...}}
```

**Explore the API:** `http://localhost:8000/docs`

## Contributing

See [`CLAUDE.md`](CLAUDE.md) for architecture decisions, coding conventions, and what's in scope for each phase. If you're using Claude Code, it will load this automatically.

## License

MIT
