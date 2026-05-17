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

## What is NOT in scope yet (don't build it ahead of its phase)

- ~~Audio/video blob streaming~~ — Done (Phase 2c). `IngestContext.stream_blob()` implemented; audio/video handlers stream to disk instead of loading into RAM.
- ~~Image OCR multilingual support~~ — Done (Phase 2c). Per-request `ocr_languages` override via `ctx.config`, EasyOCR allowlist, LRU-bounded reader cache.
- **Phase 2 closed (2026-05-09).** All six P0–P3 follow-up items shipped + Opus audit closure (H-P1b retry outbox, M-P0/M-P1b/M-P2a hardening). 225 tests passing. See [`docs/phase2c-audit.md`](docs/phase2c-audit.md). Key integration-test learnings (apply to all future tests against the running app): `ASGITransport` does NOT fire the ASGI lifespan, so manually set `app.state.arq_pool` and call `registry.discover()`; cleanup must create a fresh `create_async_engine()` to avoid "Future attached to a different loop" from cross-`asyncio.run()` connection reuse.

- **Deferred from Phase 2 to Phase 3 (LOW severity, theoretical):**
  - **L-1**: Retry endpoint `retry_payload.get("task")` will raise `AttributeError` → 500 if `doc.error.retry_payload` is somehow stored as a non-dict. `_fail_document` always writes a dict, so this is theoretical, but adding `if not isinstance(retry_payload, dict)` would harden it.
  - **L-2**: `tests/integration/test_e2e_pipeline.py::test_worker_queue_name_matches_api_pool_default` uses the literal `"arq:queue"` instead of `arq.connections.ArqRedis.default_queue_name`. If arq ever changes its default in a future version, the test would silently pass while a new wiring bug emerges. (Mitigated: `test_job_lands_in_arq_queue` does the empirical round-trip.)
- **Phase 3 closed (2026-05-10).** All 3 HIGH issues (H-1 TextBlock filter, H-2 startup warmup, H-3 E2E enrichment assertions) fixed; L-3 chunk_faithfulness metric implemented. 283 tests passing. See [`docs/phase3-audit.md`](docs/phase3-audit.md). M-1/M-2/M-3 closed by Phase 4 (routing enforcement, tenant policy fetch, matched-rule audit). M-4/M-5 remain deferred:
  - **M-4 (Per-doc language detect)**: lingua runs N times for N chunks today. Most docs are monolingual — sample-and-propagate would be cheaper. Performance only.
  - **M-5 (Anthropic client caching)**: client instantiated per `summarize_document` call — cache module-level keyed on api_key when LLM enrichment goes high-volume.
- **Phase 4 audit closed 2026-05-14.** All 2 CRITICAL + 5 HIGH issues from `docs/phase4-audit.md` fixed. 329 tests passing.
- **Deferred from Phase 4 to Phase 5:**
  - **M-1 (E2E routing test)**: no integration test exercises a custom `routing_policy` with `sinks=[]` end-to-end.
  - **M-3 (BM25 sinks filter)**: BM25 search does not filter by `sinks`, so explicitly dropped chunks remain text-searchable. Irrelevant for default policy; matters for custom policies with `sinks=[]`.
  - **M-4 (`create_tenant.test` dead code)**: `CreateTenantRequest` has no `test` field; test-namespaced keys cannot be minted via admin.
  - **M-5 (Lua script re-register)**: `r.register_script(_LUA_SCRIPT)` still runs per call (cheap SHA1 compute). Hoist to module level when it becomes measurable overhead.
  - **L-1/L-2/L-3** — JWT `iss`/`aud` claims not enforced; `expires_at.astimezone(UTC)` fix already applied (L-2 closed); `reset_after_seconds` off-by-one (always overestimates by ≤1 s, errs safe).
- Docling PDF pilot — gated behind a feature flag, never the default
- ColPali / visual retrieval — deferred (trigger: recall@10 gap > 15 %)
- RAPTOR / GraphRAG — deferred (trigger: > 20 % multi-hop queries)

Note: hybrid search (BM25 + pgvector + RRF) was pulled into Phase 2 alongside embeddings. The Phase 3 enrichment scope (LLM, NER, routing) is unchanged.

If you find yourself reaching for one of these before the phase is ready, stop and check with the team.

---

## Open architectural questions (do not resolve unilaterally)

These are documented in `docs/architecture.md §9`. The most load-bearing ones:

- **Q4** — PyMuPDF is AGPL-3.0. If we distribute on-prem, legal obligations attach. Don't expand PyMuPDF usage without confirming the distribution model.
- **Q13** — Docling adoption threshold: < 10% win on gold eval set → keep PyMuPDF as default.
- **Q14** — Visual retrieval trigger: recall@10 gap > 15% → then consider Qdrant.
- **Q16** — Eval gold-set ownership: engineering owns it, versioned under `eval/fixtures/`.
- **Q17** — Image OCR backend: PaddleOCR 3.x rejected — ONEDNN/PIR executor incompatibility on consumer Intel/AMD CPUs (2026-05). **EasyOCR (Apache 2.0, PyTorch-based) is the current default.** Re-evaluate PaddleOCR when they release a PIR-stable CPU wheel.
- **Q18** — **Resolved (2026-05-08).** `IngestContext.stream_blob() → AsyncIterator[bytes]` implemented in `pipeline/context.py`. Audio and video handlers now stream to temp file instead of buffering the full blob. `MAX_GPU_INPUT_BYTES` guard removed from both handlers. `_DiskCtx` in `eval/runner.py` updated with a chunked local implementation.
- **Q19** — **BGE-M3 switch BLOCKED (2026-05-08 decision).** Phase 2c expanded the corpus to 53 chunks (7 fixtures, 39 queries). Paired t-test on recall@1: t = +0.514 (bge-base: 0.361, bge-m3: 0.332), nDCG@3: t = +0.880 — **neither metric is statistically significant at p<0.05** (would require |t| > 2.024). Truthful conclusion: **the two models are statistically indistinguishable on this corpus.** The 9-chunk +18.7% advantage was sampling noise; the 53-chunk result confirms there is no measurable retrieval-quality benefit to BGE-M3 that would justify the destructive vector(768)→vector(1024) + HNSW rebuild. **BGE-base-en-v1.5 (768-dim) remains the production model.** Re-evaluate if a qualitatively different retrieval failure pattern emerges in production (e.g., multilingual queries where BGE-M3's broader training would matter). See `eval/results/README.md` for full results.

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

## Phase roadmap (current: Phase 6 — Hardening)

| Phase | What | Status |
|---|---|---|
| 0 — Skeleton | FastAPI, ARQ, Postgres schema, eval harness | Done |
| 1 — Text formats | PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX handlers + chunker + upload API | Done |
| 2 — Embeddings + hybrid search | BGE-base-en-v1.5 (local) + pgvector + BM25/vector/RRF endpoint | **Done** — 125 unit tests, 6 E2E tests, 5/5 eval fixtures. Audit closed 2026-05-07. See [`docs/phase2-audit.md`](docs/phase2-audit.md). |
| 2b — Heavy formats | Audio (faster-whisper), video (ffmpeg), image OCR (EasyOCR), GPU worker queue, embedding bakeoff | **Done** — 176 unit tests, 6 E2E. Bakeoff: BGE-M3 +18.7% recall@1 vs BGE-base. Audit closed 2026-05-07. See [`docs/phase2b-audit.md`](docs/phase2b-audit.md). |
| 2c — Robustness & multilingual | `IngestContext.stream_blob()`, multilingual OCR (per-request lang override + LRU-bounded reader cache + allowlist), expanded eval corpus (53 chunks / 39 queries), backpressure (HTTP 429), idempotency guard, DLQ retry payload, queue-name alignment fix | **Done** — 164 unit tests, 6 E2E. Bakeoff verdict: bge-base ≈ bge-m3 (not significant, t=0.514). Pre-existing CPU queue-name mismatch fixed. Audit closed 2026-05-08. |
| 2c — Final closure | All six P0–P3 follow-ups: E2E pipeline test (P0), `stream_blob()` integration test (P1), `POST /v1/documents/{id}/retry` (P1), GPU queue backpressure (P2), atomic claim in `_run_ingest` for stronger idempotency (P2), bakeoff fixture cleanup — md-003 WAL + txt-001 multi-chunk (P3). Opus audit found H-P1b (retry endpoint orphaned docs on Redis failure); fixed with transactional outbox. Three M-level hardening fixes: queue-name regression guard, retry task allowlist, `GPU_QUEUE_NAME` constant centralized. | **Done** — 225 unit + integration tests passing, ruff clean, eval harness 8/9 (txt-002 67% pre-existing). Phase 2 fully closed 2026-05-09. |
| 3 — Enrichment | Language detection (lingua), NER (spaCy `en_core_web_sm`), LLM summarization scaffold (Claude Haiku, gated on `ANTHROPIC_API_KEY`), routing policy engine (declarative rules), `chunk_faithfulness` metric in eval harness, API: `summary` + `routing_decision` on doc, `GET /v1/documents/{id}/entities` | **Done — 2026-05-10.** 283 tests passing, ruff clean, eval 8/9. Audit closed by Opus 4.7. See [`docs/phase3-audit.md`](docs/phase3-audit.md). 5 MEDIUM items deferred to Phase 4 (routing enforcement, tenant policy fetch, matched-rule audit, per-doc lang detect, Anthropic client caching). |
| 4 — Multi-tenant | API keys (Argon2id), RS256 JWT exchange, fine-grained scopes, Lua token-bucket rate limiter, FORCE ROW LEVEL SECURITY on 6 tables, admin/tenant self-service endpoints. M-1/M-2/M-3 closed (routing enforcement, tenant policy fetch, matched-rule audit). | **Done — 2026-05-14.** 329 tests passing, ruff clean. Opus audit (`docs/phase4-audit.md`) closed: C-1 (`admin_session` now commits on clean exit), C-2 (version-counter cache invalidation), H-1 (RLS GUC set/reset in `get_db_for_tenant`), H-2 (`rate_limited` dependency on all routes), H-3 (Redis singleton in lifespan), H-4 (`VerificationError` catch in `verify_key`), H-5 (`UpdateTenantRequest` Pydantic model with policy validation), M-2 (positive admin provisioning integration test). |
| 5 — Observability | OTel traces (spans in API + worker → Tempo via OTLP), prometheus-client metrics (`/metrics` scrape → Prometheus), structlog JSON → Promtail → Loki, Grafana dashboards (Pipeline Overview / Search / Tenants) auto-provisioned. W3C traceparent propagation across API→ARQ boundary. `docker compose --profile monitoring up -d`. Tempo v3 config fix (compactor stanza removed). | **Done — 2026-05-18.** 346 tests passing, ruff clean. NEW-H-1 GUC pool-leak fix (Opus re-audit) included. |
| 6 — Hardening | Chaos tests, DB partitioning, cost dashboards | Ongoing |
