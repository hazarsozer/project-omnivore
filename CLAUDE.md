# Omnivore — CLAUDE.md

This file is loaded automatically by Claude Code. Read it before touching any code.

## What this project is

Omnivore is a **universal document ingestion pipeline**. It accepts any file format, extracts structured intelligence from it, and stores the results in PostgreSQL so they can be searched semantically or queried like a database.

Full architecture rationale lives in `docs/architecture.md`. Read it before making any structural decisions.

---

## Locked tech stack

Do not propose replacements for these without a concrete reason documented in `docs/architecture.md §9`.

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Package manager | `uv` — use `uv add`, not `pip install` |
| Web framework | FastAPI 0.110+ |
| Job queue | ARQ + Redis 7 |
| Database | PostgreSQL 16 + pgvector 0.7+ |
| Object storage | MinIO (dev) / S3 (prod) — same S3 API |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic |
| Logging | structlog — never use `print()` or stdlib `logging.info()` directly |
| Config | pydantic-settings via `get_settings()` — never read `os.environ` directly |

---

## Project layout

```
src/omnivore/
  api/              FastAPI app, routes, schemas
  db/               SQLAlchemy models, session factory, Alembic migrations in alembic/
  pipeline/         The extraction + chunking core
    models.py       NIR dataclasses (ExtractionResult, Block, Fragment, Chunk, …)
    context.py      BlobRef + IngestContext
    registry.py     FormatHandler protocol + HandlerRegistry
    chunker.py      Structure-first chunker
    handlers/       One file per format (pdf.py, docx.py, …)
  worker/
    context.py      WorkflowContext protocol + ArqWorkflowContext
    tasks.py        ARQ task functions (ingest_dispatch, …)
    main.py         WorkerSettings entry point
eval/               Eval harness + fixture catalog (run before shipping any handler change)
```

---

## Rules that shape this codebase

### Handlers

Every format handler must:
1. Implement the `FormatHandler` protocol from `omnivore.pipeline.registry` — `name`, `version`, `accepts`, `cost_class`, `timeout_seconds`, and `async def extract(blob, ctx) -> ExtractionResult`.
2. Be registered in `pyproject.toml` under `[project.entry-points."omnivore.handlers"]`.
3. **Never import ARQ symbols**. Use `IngestContext` and `WorkflowContext` only. This is the migration seam for swapping ARQ → Temporal later.
4. Call `await ctx.read_blob()` to get file bytes — never read from disk or open MinIO directly.
5. Return a populated `ExtractionResult` with `blocks` (for document-like sources) and `fragments`. Sources with no inherent hierarchy leave `blocks=[]`.

### NIR models

Pipeline-internal models (`Block`, `Fragment`, `ExtractionResult`, `Chunk`, etc.) are **dataclasses** in `pipeline/models.py`. Do not make them Pydantic models — they are never HTTP-serialized.

API request/response models are **Pydantic** `BaseModel` subclasses in `api/schemas.py`.

### API responses

Every endpoint returns the `APIResponse[T]` envelope:
```python
{"success": bool, "data": T | null, "error": {"code": str, "message": str} | null, "meta": dict | null}
```
Never return bare dicts or raw lists from route handlers.

### Database

- All tables live in the `core` PostgreSQL schema.
- Schema changes go through Alembic migrations — never `CREATE TABLE` or `ALTER TABLE` by hand.
- Write migrations as raw SQL via `op.execute()` for precision (not autogenerate).
- Use SQLAlchemy 2.0 `Mapped` / `mapped_column` style — not the legacy `Column()` style.

### Chunker

The chunker (`pipeline/chunker.py`) is **structure-first**: section boundaries take precedence over token limits. Never split a chunk across a heading boundary. Token overflow (512 tokens max, 64 overlap) only applies *within* a section. Chunk metadata (`heading_path`, `source_block_ids`, `table_lineage`, `position`) is mandatory — never produce a chunk without it.

### Logging

```python
import structlog
logger = structlog.get_logger(__name__)
logger.info("event.name", key=value, other_key=other_value)
```

Event names use `dot.notation`. Always log `document_id` on events related to a document.

### Config

```python
from omnivore.config import get_settings
settings = get_settings()  # cached singleton
```

Never call `Settings()` directly outside of `config.py`. Never read `os.environ` directly.

---

## What is NOT in Phase 1 (don't build it yet)

- Audio/video handlers (`faster-whisper`, `ffmpeg`) — Phase 2
- Image OCR (PaddleOCR) — Phase 2
- Embeddings (OpenAI / BGE-M3) — Phase 2 (bakeoff first)
- LLM summarization, NER, sentiment — Phase 3
- Routing policy engine (jsonlogic) — Phase 3
- Real hybrid search (BM25 + vector + RRF) — Phase 3
- Multi-tenant auth (JWT, API keys, RLS) — Phase 4
- Docling PDF pilot — Phase 1+ (behind feature flag, not default)
- ColPali / visual retrieval — deferred (trigger: recall@10 gap > 15%)
- RAPTOR / GraphRAG — deferred (trigger: > 20% multi-hop queries)

If you find yourself reaching for one of these before the phase is ready, stop and check with the team.

---

## Open architectural questions (do not resolve unilaterally)

These are documented in `docs/architecture.md §9`. The most load-bearing ones:

- **Q4** — PyMuPDF is AGPL-3.0. If we distribute on-prem, legal obligations attach. Don't expand PyMuPDF usage without confirming the distribution model.
- **Q13** — Docling adoption threshold: < 10% win on gold eval set → keep PyMuPDF as default.
- **Q14** — Visual retrieval trigger: recall@10 gap > 15% → then consider Qdrant.
- **Q16** — Eval gold-set ownership: engineering owns it, versioned under `eval/fixtures/`.

---

## Before you commit

Run the eval harness on any fixture that's relevant to your change:
```bash
python -m eval.harness --fixture-ids pdf-001 pdf-003
```

Checklist:
- [ ] `ruff check src/` passes
- [ ] `pyright src/` passes (or new errors are justified)
- [ ] New handler is registered in `pyproject.toml` entry points
- [ ] Migration added if schema changed
- [ ] `eval/` fixtures cover the format you touched

---

## How to run locally

```bash
# 1. Install deps
uv sync

# 2. Start infrastructure
docker compose up -d postgres redis minio

# 3. Run migrations
alembic upgrade head

# 4. Start API (dev, with reload)
uvicorn omnivore.api.main:app --reload

# 5. Start worker (separate terminal)
python -m omnivore.worker.main

# 6. Upload a file
curl -X POST http://localhost:8000/v1/documents \
     -F "file=@path/to/your.pdf"

# 7. Poll status
curl http://localhost:8000/v1/documents/{document_id}
```

---

## Phase roadmap (current: Phase 1 complete)

| Phase | What | Status |
|---|---|---|
| 0 — Skeleton | FastAPI, ARQ, Postgres schema, eval harness | Done |
| 1 — Text formats | PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX handlers + chunker + upload API | Done |
| 2 — Heavy formats | Audio (faster-whisper), video (ffmpeg), image OCR, embedding bakeoff | Next |
| 3 — Enrichment | LLM summary, NER, routing policy, hybrid search | — |
| 4 — Multi-tenant | API keys, JWT, rate limiting, RLS | — |
| 5 — Observability | OTel, Prometheus, Grafana, Loki | — |
| 6 — Hardening | Chaos tests, DB partitioning, cost dashboards | Ongoing |
