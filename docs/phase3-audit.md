# Phase 3 Audit — 2026-05-10

**Reviewer:** Opus 4.7 (Senior Systems Architect role)
**Subject:** Sonnet 4.6's Phase 3 implementation
**Verdict:** **PASS** — All 3 HIGH issues fixed (2026-05-10 by Sonnet 4.6); L-3 (chunk_faithfulness) implemented; 5 MEDIUM items documented as Phase 4 deferrals. Phase 3 closed.

---

## Scope built

- `src/omnivore/pipeline/enrichers/` — `language.py` (lingua), `summarizer.py` (Claude Haiku scaffold, no-op without `ANTHROPIC_API_KEY`), `ner.py` (spaCy `en_core_web_sm`)
- `src/omnivore/pipeline/routing.py` — declarative rule engine, `evaluate_policy()` + `validate_policy()`
- `src/omnivore/worker/tasks.py` — wired enrichment into `_run_ingest` (language → chunks; NER → entities; summary → `doc.doc_metadata.summary`; routing → `doc.routing_decision`)
- `src/omnivore/api/routes/documents.py` — `summary` + `routing_decision` in GET response; new `GET /v1/documents/{id}/entities`
- 53 new unit tests; 234 unit + 44 integration = 278 passing; ruff clean

---

## HIGH — Blocking (must fix before Phase 3 closes)

### H-1: Anthropic content type assumption (latent crash)

**Where:** `src/omnivore/pipeline/enrichers/summarizer.py:76`

```python
raw = message.content[0].text.strip()
```

**Problem:** `message.content[0]` is a union of ~12 block types. Only `TextBlock` has `.text` — `ThinkingBlock`, `ToolUseBlock`, `RedactedThinkingBlock`, etc. do not. Pyright catches this with 11 errors. Today, Haiku without thinking returns a `TextBlock` first, so it works. The minute extended thinking is enabled on this call (a single param flip), `content[0]` becomes a `ThinkingBlock` and the access raises `AttributeError`. The broad `except Exception` swallows it as "summarization failed" — silent failure in production.

**Fix:** Filter for the first text block:
```python
text_blocks = [b for b in message.content if getattr(b, "type", None) == "text"]
if not text_blocks:
    return None
raw = text_blocks[0].text.strip()
```

---

### H-2: Models not warmed at worker startup

**Where:** `src/omnivore/worker/tasks.py:on_startup`

`_detector()` (lingua) and `_nlp()` (spaCy) are `lru_cache`-decorated, so the first call loads them. Measured: spaCy `en_core_web_sm` = **1.13s**, lingua n-gram load happens on first detection call (full set of 16 languages = several hundred MB). The first job after a worker restart blocks the event loop during this load.

The pattern is already established in this codebase: `pipeline/embeddings.py:_get_model()` is warmed in `on_startup` to download BGE-base before the first job. Phase 3 didn't extend this.

**Fix:**
```python
async def on_startup(ctx: dict) -> None:
    get_settings()
    registry.discover()
    from omnivore.pipeline.embeddings import _get_model
    from omnivore.pipeline.enrichers.language import _detector, detect_language
    from omnivore.pipeline.enrichers.ner import _nlp
    _get_model()
    _nlp()                           # 1.1s spaCy load
    _detector()                      # build detector
    detect_language("warmup text " * 5)  # force lingua n-gram load
    logger.info("worker.startup", handlers=len(registry.all_handlers()))
```

---

### H-3: Zero E2E assertions for Phase 3 enrichment

**Where:** `tests/integration/test_e2e_pipeline.py`

The E2E test runs upload→worker→search but never asserts:
- `chunk.language` is populated (for English fixtures, expect `"en"`)
- Any `entities` row was created
- `doc.doc_metadata["summary"]` (only when key present, but at minimum that the path doesn't crash)
- `doc.routing_decision["sink_counts"]` is non-empty

**Risk:** the wiring in `_run_ingest` could silently regress (e.g., enrichment block accidentally skipped, an exception swallowed early) and the green test suite would not flag it. Unit tests cover each enricher in isolation; the wire-up is unverified.

**Fix:** Add at least one E2E assertion block after the existing search check:
```python
# Phase 3 enrichment assertions
assert chunk_row.language == "en", f"language not detected, got {chunk_row.language!r}"
ents = (await db.scalars(select(Entity).where(Entity.document_id == doc_id))).all()
assert len(ents) >= 1, "NER produced no entities for English fixture"
doc_after = await db.get(Document, doc_id)
assert doc_after.routing_decision is not None
assert doc_after.routing_decision.get("sink_counts"), "routing_decision empty"
```

---

## MEDIUM — Acceptable for Phase 3 v1 if documented

### M-1: Routing decision computed but not enforced

**Where:** `tasks.py::_compute_routing_decision` and `routing.evaluate_policy`

The engine produces a `sink_counts` summary on the document but writes to chunks/entities/tables happen unconditionally before. Architecture §2.4 says `transcript` with `confidence_lt: 0.6` → `sinks: []` should DROP the chunk. Currently kept.

**Resolution:** Phase 3 v1 ships the engine as informational/observability. Document explicitly that enforcement is Phase 4 work tied to multi-tenant policy.

### M-2: No tenant policy fetch

**Where:** `tasks.py::_compute_routing_decision`

Hardcoded to `DEFAULT_POLICY`. The `tenants.config` JSONB column is never read. Architecture mentions `PUT /v1/tenants/{id}/policy`. Sonnet did not implement this.

**Resolution:** Phase 4 work (multi-tenant). Document.

### M-3: No matched-rule-id stored per chunk

Architecture §2.4: "Each fragment carries the matched rule id in its audit row." Currently `evaluate_policy` returns the sink set but not which rule matched. Phase 4 work; flag.

### M-4: Per-chunk language detection is wasteful

`detect_language(chunk.content)` runs N times for N chunks. Most docs are monolingual. Should detect once on a representative sample (e.g., concatenated first 2 KB of all chunks) and assign to all chunks unless multilingual handling becomes a real requirement.

**Resolution:** Performance optimization. Acceptable for v1.

### M-5: Anthropic client instantiated per call

`AsyncAnthropic(api_key=api_key)` runs inside `summarize_document` — no client reuse, no connection pooling. Pattern in this codebase is to cache (e.g., `_get_model()` in embeddings). Cache at module level keyed on api_key.

**Resolution:** Optimization. Acceptable for v1 since it's gated on key presence.

---

## LOW — Tracked

### L-3: RAGChecker / ARES not wired into eval harness

Explicitly part of Phase 3 scope per architecture §10:
> | **3 — Enrichment** | LLM summarization, NER, language detection, routing policy engine + jsonlogic. **RAGChecker / ARES wired into eval harness.** | 1 week |

Sonnet acknowledged this was deferred. The biggest scope gap. Adds answer-faithfulness and retrieval-quality metrics to `eval/harness.py`. Requires reference corpus and grounding logic — substantial work.

**Resolution:** Phase 3 follow-up session, before Phase 4 starts.

### L-4: NER try/except can persist partial entities

If `db.add(EntityRow(...))` raises mid-loop, the partial entities added before the exception are committed when the surrounding `await db.commit()` runs (post-summary). Better pattern: flush entities in a savepoint and rollback the savepoint on exception. Minor.

### L-5: Tests use private `_KEPT_LABELS`

`tests/unit/test_enrichers_ner.py` imports `_KEPT_LABELS`. Either expose as `KEPT_LABELS` or remove the test that uses it directly. API hygiene only.

---

## Verified — No issue

- `core.documents.routing_decision` JSONB column exists in `0001_initial_schema.py` (no migration needed). ✅
- `core.entities` UniqueConstraint `(document_id, label, normalized)` is satisfied by Sonnet's NER deduplication. ✅
- Atomic claim in `_run_ingest` prevents duplicate enrichment runs. ✅
- `summarize_document` returns `None` cleanly when API key is absent or content is empty — graceful no-op. ✅
- Claude Haiku model ID `claude-haiku-4-5-20251001` is correct. ✅
- `Chunk` (pipeline dataclass) and `ChunkRow` (DB model) imports do not collide in `tasks.py`. ✅
- `TYPE_CHECKING` import for `Chunk` in enrichers avoids circular imports. ✅

---

## Recommended next session plan (hand back to Sonnet)

**Goal:** Close Phase 3 fully.

1. **Fix H-1**: filter `TextBlock` in `summarizer.py:76` before reading `.text`. Add a unit test that mocks `message.content` with a non-text block first.
2. **Fix H-2**: warm `_nlp()`, `_detector()`, and trigger lingua first detection inside `on_startup` in `worker/tasks.py`. Mirror in `worker/gpu_main.py` if the GPU worker also runs enrichment (verify).
3. **Fix H-3**: add Phase 3 enrichment assertions to `tests/integration/test_e2e_pipeline.py` — language on chunks, entity rows present, routing_decision populated.
4. **Address L-3**: implement minimal RAGChecker-style faithfulness scoring in `eval/harness.py`. At minimum: for each gold query, compute whether the top-k retrieved chunk(s) contain the gold answer span. Defer full ARES integration.
5. **Document M-1, M-2, M-3** in CLAUDE.md "What is NOT in scope yet" — explicitly mark routing enforcement, tenant policy fetch, and matched-rule-id audit as Phase 4 deferrals.
6. **Run preflight after fixes:**
   ```bash
   docker compose up -d postgres redis minio
   uv run pytest tests/ -q                      # expect 280+ passed
   uv run ruff check src/ tests/ eval/          # clean
   uv run python -m eval.harness                # 8/9 minimum (txt-002 pre-existing)
   ```
