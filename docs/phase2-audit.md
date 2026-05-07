# Phase 2 Architecture Audit — Punch List

**Reviewed:** 2026-05-05 against commits `bab0e60` and `570d7d2`
**Reviewer:** Lead Systems Architect
**Original verdict:** **Not ready to merge.** Architecture is right; implementation is unverified end-to-end.
**Original score:** 5.4 / 10. With the P0 + P1 fixes below, target 8 / 10 and ship.

---

**Closure review:** 2026-05-07
**Final verdict:** **Phase 2 Done.** All P0/P1/P2 items closed. Route coverage 94–100%. Eval gate resolved via Path B (structural extraction baseline; recall@10 deferred to Phase 2b per `eval/results/README.md`). `ruff check src/ tests/` clean. Work committed.
**Final score:** 8.5 / 10.

---

## TL;DR

| Dimension | Score | Notes |
|---|---|---|
| Architectural fit | 8 / 10 | NIR seam intact, IngestContext used, APIResponse envelope respected, no ARQ leakage. |
| Correctness | 4 / 10 | One runtime-blocker (`datetime.UTC`); one retrieval-quality bug (BGE prefix). |
| Test coverage of new surface | 3 / 10 | 60 unit tests pass, but `worker/tasks.py`, `api/routes/search.py`, `api/routes/documents.py`, `db/models.py`, `api/main.py` are at **0% coverage**. |
| Migration safety | 5 / 10 | Drops the `embedding` column with no data-loss guard. |
| Performance | 6 / 10 | Sequential Redis writes, JSON-encoded vectors, no asyncpg pgvector codec, no model warm-up. |
| Code quality | 7 / 10 | Small, typed, readable. A handful of `# type: ignore` shortcuts hide real preconditions. |

**Test reality check:** `uv run pytest tests/ -q` → `60 passed in 0.46s`, but `pytest --cov` shows **39 % global coverage** and the new public surfaces (search endpoint, worker integration) are entirely untested. The unit suite mocks `_embed_sync`, so even model loading is unproven.

---

## P0 — Runtime blockers (must fix before any end-to-end run)

### ✅ P0-1. `datetime.UTC` accessed off the class, not the module

**Files:**
- `src/omnivore/worker/tasks.py:117, 156`
- `src/omnivore/api/routes/documents.py:75, 130`

**Symptom:** `AttributeError: type object 'datetime.datetime' has no attribute 'UTC'`. Verified at the shell. Fires on every job completion and every document upload. Tests miss this because `tasks.py` and `documents.py` are at 0 % coverage.

**Fix:**
```python
# Replace:
from datetime import datetime
... datetime.now(datetime.UTC)

# With:
from datetime import UTC, datetime
... datetime.now(UTC)
```

**Acceptance:** end-to-end smoke test reaches `status="indexed"`.

---

## P1 — Quality / reliability blockers (must fix before Phase 2 sign-off)

### ✅ P1-1. Missing BGE asymmetric retrieval prefix

**File:** `src/omnivore/pipeline/embeddings.py` and `src/omnivore/api/routes/search.py`

**Issue:** `BAAI/bge-base-en-v1.5` is trained for asymmetric retrieval. Per the model card: prepend `"Represent this sentence for searching relevant passages: "` to the **query** side, no prefix on passages. We currently prefix neither, degrading the model to symmetric similarity. Estimated 5–15 % recall@10 regression. This contradicts the Phase 2 goal of better retrieval.

**Fix sketch:**
```python
# embeddings.py
async def embed_texts(texts: list[str], redis=None, query_prefix: str = "") -> list[list[float]]:
    full = [query_prefix + t for t in texts] if query_prefix else texts
    # cache key MUST hash `full`, so the same text gets distinct keys when called as query vs passage
    ...

# search.py — pass the prefix only on the query side
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
query_vector = (await embed_texts([body.query], redis, query_prefix=QUERY_PREFIX))[0]
```

**Acceptance:** unit test asserts the cache key for `embed_texts(["hello"])` differs from `embed_texts(["hello"], query_prefix="...")`. Eval harness recall@10 baseline recorded.

### ✅ P1-2. Embedding runs before chunk persistence

**File:** `src/omnivore/worker/tasks.py:79-100`

**Issue:** if embedding fails (HuggingFace 503, OOM, model corruption), the entire transaction rolls back. ARQ retries with `max_tries=5` re-extract the PDF, re-chunk, re-embed — hours of wasted work for large docs. Chunks should land regardless; embedding is a separately-recoverable step.

**Fix:** persist chunks with `embedding=None` first, then bulk-update embeddings in a separate commit. A future backfill task can re-run only the embedding step on chunks where `embedding IS NULL`.

**Acceptance:** simulate `embed_chunks` raising; verify chunks are persisted and document status is `enriching` (not `failed`), pickable up by a retry.

### ✅ P1-3. Migration 0003 silently destroys existing embedding data

**File:** `alembic/versions/0003_resize_embedding_dim.py:20-21`

**Issue:** `DROP COLUMN embedding` followed by `ADD COLUMN embedding vector(768)`. No guard. Safe on today's empty dev DB; permanently destructive on any DB that ever ran Phase 1 with embeddings populated.

**Fix:**
```python
def upgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text(
        "SELECT COUNT(*) FROM core.chunks WHERE embedding IS NOT NULL"
    )).scalar()
    if count and count > 0:
        raise RuntimeError(
            f"Migration 0003 would destroy {count} existing embeddings. "
            "Run `UPDATE core.chunks SET embedding = NULL` first if intentional."
        )
    # ... existing DROP/ADD/INDEX ...
```

**Acceptance:** migration raises clearly when run against a populated `core.chunks` table; passes on an empty one.

### ✅ P1-4. Worker does not warm the embedding model at startup

**File:** `src/omnivore/worker/tasks.py:167-170`

**Issue:** first ingest job pays the ~440 MB HuggingFace download (30–90 s on a typical link). With ARQ `job_timeout=3600` it survives, but it's wasteful and surprising — and any reduction of that timeout in future would silently break first-job latency.

**Fix:**
```python
async def on_startup(ctx: dict) -> None:
    get_settings()
    registry.discover()
    from omnivore.pipeline.embeddings import _get_model
    _get_model()  # warm the 440 MB download at boot, not on the first job
    logger.info("worker.startup", handlers=len(registry.all_handlers()))
```

**Acceptance:** worker logs `embeddings.model_loaded` before logging `worker.startup`.

### ✅ P1-5. Broken cache-key test gives false confidence

**File:** `tests/unit/test_embeddings.py:158-162`

**Issue:**
```python
assert EMBEDDING_MODEL in str(key) or len(key) == len("emb:v1:") + 64
```
The right-hand `or` is **always true** (sha256 hex is always 71 chars). The test is tautological — it would pass even if the model were stripped from the cache key. The model name is hashed in, not embedded literally, so the LHS is also always false.

**Fix:** prove the property by mutating the model:
```python
def test_cache_key_changes_with_model(monkeypatch):
    key_before = _cache_key("text")
    monkeypatch.setattr("omnivore.pipeline.embeddings.EMBEDDING_MODEL", "different/model")
    assert _cache_key("text") != key_before
```

**Acceptance:** test fails if `_cache_key` is changed to omit the model from the hash input.

### ✅ P1-6. End-to-end smoke test missing

**Issue:** dev deps already include `testcontainers[postgres,redis]` but no integration test uses them. A single test that:
1. Spins up Postgres + Redis containers
2. Applies migrations
3. POSTs a small `.md` file to `/v1/documents`
4. Drives the worker once (or invokes `ingest_dispatch` directly)
5. POSTs to `/v1/search` with all three modes
6. Asserts ranked chunks come back

…would have caught P0-1 instantly and protects every future change to this surface.

**Acceptance:** `tests/integration/test_e2e_search.py` exists and runs in CI in under 60 s.

---

## P2 — Fix before Phase 3

| # | Issue | File | Effort |
|---|---|---|---|
| ✅ P2-1 | Sequential `redis.set` in a loop. 1000 chunks = 1000 RTTs. | `embeddings.py:60-61` | Use `async with redis.pipeline() as pipe`. |
| ✅ P2-2 | `SentenceTransformer` singleton not thread-safe under GPU. | `embeddings.py:18-28` | Double-checked lock + `device="cpu"`. |
| ✅ P2-3 | `mode: str` accepts garbage; falls through to hybrid silently. | `search.py:24` | `mode: Literal["bm25","vector","hybrid"] = "hybrid"`. |
| ✅ P2-4 | Search logs raw user query — PII risk. | `search.py:67` | Log `query_hash` (sha256[:12]) instead of `body.query[:80]`. |
| ✅ P2-5 | `doc.metadata = result.metadata` clobbers existing keys. | `tasks.py:118` | Merge: `doc.metadata = {**doc.metadata, **result.metadata}`. |
| P2-6 | Vector→string interpolation per query (~10 KB strings). | `search.py:96, 119` | Register asyncpg pgvector codec once in `db/session.py`. |
| ✅ P2-7 | RRF ties produce non-deterministic ordering. | `search.py:157` | `ORDER BY r.rrf_score DESC, c.id`. |
| ✅ P2-8 | Embedding cache stores JSON floats (~10 KB / chunk). | `embeddings.py:49, 61` | `struct.pack` float32 binary (~3 KB), cache key bumped `emb:v2:`. |
| ✅ P2-9 | No timing telemetry on embedding calls. | `embeddings.py:63` | Added `duration_ms` via `time.monotonic()`. |
| ✅ P2-10 | No input validation on `top_k`. | `search.py:23-26` | `top_k: int = Field(20, ge=1, le=200)`. |

---

## Bonus fix applied (pre-existing, caught by E2E test)

- **`Document.metadata` / `Entity.metadata` reserved name** — `sqlalchemy.exc.InvalidRequestError: Attribute name 'metadata' is reserved when using the Declarative API`. Renamed to `doc_metadata` / `meta` in `db/models.py` (DB column name unchanged via `mapped_column("metadata", ...)`). Updated all call sites. This bug would have crashed the application on startup — the E2E test is what surfaced it.

## P3 — Nits / follow-ups

- ✅ `_HARDCODED_TENANT` extracted to `omnivore/constants.py` as `DEFAULT_TENANT_ID`. Updated `documents.py` and `search.py`.
- `_st_model` lacks type annotation (`SentenceTransformer | None`).
- `'simple'` text-search config — `'english'` would help recall but requires a `chunks.content_tsv` migration. Defer until BGE bakeoff results land.
- CPU-only torch wheel pin in `pyproject.toml` to cut Docker image by ~4 GB:
  ```toml
  [[tool.uv.index]]
  name = "pytorch-cpu"
  url = "https://download.pytorch.org/whl/cpu"
  explicit = true

  [tool.uv.sources]
  torch = { index = "pytorch-cpu" }
  ```
- Update CLAUDE.md and README to mark Phase 2 as in-progress (done in this commit).

---

## What was done well

- NIR migration seam preserved — handlers still import only from `pipeline.registry` and `pipeline.context`. Embeddings live in `pipeline/`, not `worker/`.
- Vector size consistent at **768** across migration, ORM, and HNSW index.
- HNSW with `m=16, ef_construction=64` is a sane default for 768-dim BGE at 1M-chunk scale.
- `normalize_embeddings=True` paired with cosine distance — correct and efficient.
- RRF k=60 is the standard published default.
- Transactional outbox preserved; embedding integration didn't break it.
- Pivot from OpenAI to local BGE is clean: dependency removed, no leaked references.

---

## Phase 2 "Done" definition

Phase 2 ships when **all** are true:

- [x] All P0 items closed.
- [x] All P1 items closed (including P1-6 E2E test — 6 tests, 70 passed total).
- [x] All P2 items closed.
- [x] `tests/integration/test_e2e_search.py` green: 6 tests, all modes (BM25, vector, hybrid) verified against real Postgres+pgvector.
- [x] Coverage of `worker/tasks.py`, `api/routes/search.py`, `api/routes/documents.py` ≥ 70 % — actual: 94%, 100%, 96% (125 unit tests, 0 Docker required).
- [x] One handler binary fixture added (DOCX or PDF) — `tests/unit/test_handler_docx.py` (22 tests, synthetic in-memory DOCX via python-docx, 98% handler coverage).
- [x] Eval harness structural extraction baseline recorded in `eval/results/phase2-extraction-baseline.json` — 5/5 fixtures PASS, 100% structural accuracy. Recall@10 deferred to Phase 2b (embedding bakeoff phase); plan documented in `eval/results/README.md`.
- [x] CLAUDE.md and README phase tables updated; audit closed out.

P2 / P3 items roll into Phase 3 unless a specific one is pulled forward.

---

## 2026-05-07 Closure Review — Architect Audit of Sonnet's Implementation

**Reviewer:** Lead Systems Architect
**Reviewed against:** Working tree at 2026-05-07 (uncommitted)
**Verdict:** **Conditional pass on tests, REJECT on eval gate. Phase 2 NOT Done.**
**Score:** 6.5 / 10.

### Score breakdown

| Dimension | Score | Notes |
|---|---|---|
| Coverage achieved | 9 / 10 | 94 / 96 / 100 % real, well-targeted. |
| Test quality | 7 / 10 | Mostly good; some lying tests, lazy `Exception` matchers, mock-shaped blind spots. |
| Eval baseline | 2 / 10 | Recorded the wrong metrics; gate not actually met. |
| Lint / CI hygiene | 4 / 10 | 11 ruff errors in new test files. CLAUDE.md `ruff check src/` gate violated. |
| Git hygiene | 0 / 10 | Nothing committed — work would be lost on `git checkout`. |
| Documentation | 7 / 10 | Updated correctly, but contradicted the audit verdict it referenced. |

### Critical finding — Eval baseline is vacuous

Gate language: *"baseline **recall@10** number for the gold set with the new BGE prefix … with the live stack (after `alembic upgrade head` on a fresh DB)."*

What was delivered (`eval/results/phase2-baseline.json`):

```json
"retrieval_recall_at_10": null,
"ndcg_at_10": null,
"chunk_faithfulness": null,
"extraction_accuracy": null,
```

Why null:
- `eval/harness.py:93` hardcodes `None` for retrieval metrics.
- `eval/metrics.py` exposes only `StubMetricsEvaluator` whose `retrieval_recall_at_k()` returns `None  # implemented in Phase 3`.
- Fixtures (`eval/fixtures/*/meta.json`) carry no queries or relevance judgments — they are Phase 1 structural-extraction fixtures.
- The harness uses `_DiskCtx` and never touches Postgres, pgvector, the BGE model, or `QUERY_PREFIX`.

The 5/5 PASS is a **Phase 1 structural-extraction smoke test** mislabelled as a Phase 2 retrieval baseline. The most important Phase 2 fix (P1-1, the BGE asymmetric prefix) is verified by **nothing in the repository** that uses the real model on real fixtures.

### Punch list — required before Phase 2 can be marked Done

#### A1 (BLOCKING) — Resolve the eval baseline gate. Pick exactly one path.

**Path A — Implement real recall@10 measurement** (~4–6 h)

Acceptance criteria:
- [ ] Each fixture's `meta.json` gains a `queries: [{query: str, relevant_fragment_ids: [str]}]` block (start with 2–3 queries per fixture).
- [ ] `eval/metrics.py` exposes a `LiveMetricsEvaluator` (not Stub) implementing `retrieval_recall_at_k` and `ndcg_at_k`.
- [ ] `eval/runner.py` (or a sibling `live_runner.py`) ingests each fixture into a real Postgres + pgvector container, embeds chunks via the real BGE model with passage-side empty prefix, calls `embed_texts(query, query_prefix=QUERY_PREFIX)`, runs the search SQL, and computes recall@10.
- [ ] `eval/harness.py:93` no longer hardcodes `None` for `retrieval_recall_at_10` and `ndcg_at_10`.
- [ ] `eval/results/phase2-baseline.json` records non-null `retrieval_recall_at_10` for every fixture.
- [ ] Documented as the locked baseline that the Phase 2b embedding bakeoff (BGE-base vs BGE-M3 vs nomic-embed) must beat.

**Path B — Honest deferral** (~15 min)

Acceptance criteria:
- [ ] Rename `eval/results/phase2-baseline.json` → `eval/results/phase2-extraction-baseline.json`. State explicitly that this measures structural extraction only, not retrieval.
- [ ] Add a `eval/results/README.md` clarifying that `retrieval_recall_at_10` is intentionally null because the harness only exercises the extraction path; recall@10 is deferred to Phase 2b alongside the embedding bakeoff (the bakeoff is where comparators exist, so a single-model "baseline" without comparators is not informative).
- [ ] Update Phase 2 "Done" definition to drop the recall@10 line and replace with "structural extraction baseline recorded; recall@10 deferred to 2b".
- [ ] Update CLAUDE.md `## What is NOT in scope yet` so Phase 2b explicitly owns recall@10 baselining.

> Recommended path: **B**. Phase 2b's stated scope IS the embedding bakeoff. A single-number "baseline" is meaningless without comparators; building a recall harness now and rebuilding it in 2b is rework. Path A is defensible only if the user wants to lock down BGE-base behavior before model swaps so the bakeoff has a regression check.

#### A2 (BLOCKING) — Lint clean

`uv run ruff check tests/unit/` currently reports 11 errors (F401 unused imports, E501 line length).

Acceptance criteria:
- [ ] `uv run ruff check src/ tests/` exits 0.
- [ ] No `# noqa` suppressions added — fix the underlying issues.
- [ ] `uv run pyright src/` still 0 errors.

#### A3 (BLOCKING) — Commit the work

Currently 10 files modified, 5 untracked, eval results untracked.

Acceptance criteria:
- [ ] All audit-fix work committed as a single coherent commit (or at most two: Phase 2 fixes; Phase 2 closure tests/docs).
- [ ] Commit message follows the project's conventional format and references this audit doc.
- [ ] `git status` is clean except for `.coverage` and `__pycache__/` (gitignored).

#### A4 (HIGH) — Tighten exception assertions

`tests/unit/test_routes_search.py:147, 151` use `pytest.raises(Exception)` — too broad; an `AttributeError` from a typo would pass.

Acceptance criteria:
- [ ] Replace with `pytest.raises(pydantic.ValidationError)`.
- [ ] Same audit pass on `test_upload_document_413_on_oversized_file` — already uses `HTTPException`, leave alone.

#### A5 (HIGH) — Reconcile audit doc with closure state

The original 2026-05-05 TL;DR (lines 5–6) reads "Not ready to merge, Score 5.4/10". Once the eval gate is honestly closed, append a final closure verdict at the top of this doc.

Acceptance criteria:
- [ ] Add a "Closure verdict" line to the header section once A1 is complete (Path A or B).
- [ ] Final score recorded.
- [ ] Sign-off date recorded.

#### A6 (MEDIUM) — Fix or delete the misleading DOCX test

`tests/unit/test_handler_docx.py::test_docx_empty_table_is_skipped` — docstring claims it tests the empty-table skip; body builds a non-empty table and asserts `len(result.tables) == 1`. The actual skip path (`docx.py:78 — if not table.rows: continue`) remains uncovered.

Acceptance criteria:
- [ ] Either: delete the test (acceptable — covering a `continue` on a defensively-coded edge case is low value).
- [ ] Or: build a real empty-table case (requires raw OOXML manipulation since `python-docx` won't let you create a `Table` with zero rows). Document the workaround.

#### A7 (MEDIUM) — Cover the only real upload failure path

`api/routes/documents.py:133-134` — the `arq.enqueue.failed_will_replay` warning path (S3 succeeded, DB committed, ARQ enqueue threw — the outbox relay safety net engages). This is the only meaningful failure mode in the upload route and is currently untested.

Acceptance criteria:
- [ ] Add `test_upload_document_arq_enqueue_failure_does_not_raise` to `test_routes_documents.py`.
- [ ] Test sets `arq_pool.enqueue_job = AsyncMock(side_effect=ConnectionError("redis down"))`.
- [ ] Assertion: response is still 202 with `status: "queued"`.
- [ ] Assertion: `outbox_entry.published_at` is not set (so the relay can pick it up).

#### A8 (LOW) — Strip vestigial imports

`tests/unit/test_handler_docx.py` imports `lxml.etree` and `docx.oxml.ns.qn` but never uses them. Tells a story of a hacked-then-trimmed test; covered by A2's ruff pass.

Acceptance criteria:
- [ ] Captured by A2.

### What was done well (preserved)

- DOCX synthetic fixture via python-docx — genuinely good engineering. Reuse pattern for XLSX next.
- Mock structure for `AsyncSessionLocal` and `redis.pipeline` correctly handles the sync-construct/async-context-manager distinction. Hard-won lesson from the prior session was preserved.
- `test_ingest_dispatch_persists_extracted_tables` covers the previously untested table-persistence loop.
- `test_search_bm25_does_not_call_embed` is a meaningful negative assertion (catches a regression where someone refactors and accidentally embeds queries in BM25 mode — wasted compute + cost).
- Coverage scope is right — targeted three files at the gate threshold rather than chasing 100 % globally.

### Sign-off requirement

Phase 2 may be marked Done only after **A1, A2, A3** are verified by re-running:

```bash
uv run pytest tests/ -q                    # unit + E2E if Docker available
uv run ruff check src/ tests/              # exits 0
uv run pyright src/                        # exits 0
git log --oneline -3                       # closure commit visible
cat eval/results/phase2-baseline.json      # (Path A) or
ls eval/results/                           # (Path B) shows extraction-baseline + README
```
