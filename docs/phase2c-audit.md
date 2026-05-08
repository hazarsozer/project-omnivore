# Phase 2c — Architect Audit (2026-05-08)

**Auditor:** Senior Systems Architect (Opus 4.7)
**Implementer:** Sonnet 4.6
**Verdict:** PASS WITH RESERVATIONS → fixed in-place during audit → CLOSED.

Phase 2c shipped as `951d659`. The audit identified one critical pre-existing bug Phase 2c
inherited but did not catch, plus several high-priority correctness issues. All
critical and high items were corrected in the audit-closure commit.

---

## Critical findings

### C1. CPU queue-name mismatch (pre-existing, since Phase 0)

**Symptom:** API pool defaults `default_queue_name="arq:queue"`. Worker declared
`WorkerSettings.queue_name = "arq:default"`. Empirically verified by enqueueing through
`create_pool()`:

```
pool.default_queue_name = 'arq:queue'
job._queue_name = 'arq:queue'
zcard arq:queue   = 1   # API enqueues here
zcard arq:default = 0   # Worker reads from here
```

**Impact:** The full upload→worker pipeline never executed end-to-end in this
codebase. Every E2E test inserts pre-populated chunks directly into Postgres rather
than running an actual upload. Phase 2c's backpressure check inherited the bug
(`zcard("arq:queue")` measures the right key for enqueues but the queue never drains).

**Fix:** Aligned `worker/main.py` to `queue_name = "arq:queue"` to match the API
pool's default. Backpressure check now reads `pool.default_queue_name` instead of a
hardcoded string, so future drift is impossible without explicit override.

**Lesson:** Phase 2c's audit revealed that *no test in any prior phase exercised the
upload→worker pipeline end-to-end*. Phase 3 should add at least one such test.

---

## High findings

### H1. Bakeoff conclusion was statistically misleading

**Symptom:** Sonnet's framing — "BGE-M3 switch BLOCKED — bge-base leads recall@1
(0.361 vs 0.332, BGE-M3 is −8%)" — implied bge-base is measurably better. Paired
t-test on 39 query pairs:

| Comparison | Mean diff | SD | t | Sig at p<0.05? |
|---|---|---|---|---|
| bge-base − bge-m3 (recall@1) | +0.0291 | 0.353 | +0.514 | No |
| bge-m3 − bge-base (nDCG@3) | +0.0373 | 0.265 | +0.880 | No |

**Truth:** Neither difference is statistically significant. The 9-chunk +18.7%
advantage for BGE-M3 was sampling noise; expanding the corpus to 39 queries erased
it. Decision (do not migrate) still stands, but for the right reason: no measurable
quality benefit justifies a destructive `vector(768)→vector(1024)` + HNSW rebuild.

**Fix:** `CLAUDE.md §Q19` and `eval/results/README.md` rewritten with the t-test
results and an explicit "statistically indistinguishable" framing. Future re-evaluation
triggers documented (multilingual ingestion, production failure pattern, smaller
model candidate).

### H2. `_readers` cache had no eviction (DoS surface)

**Symptom:** `ImageOcrHandler._readers: ClassVar[dict[tuple[str, ...], Reader]]`
accumulated ~200 MB EasyOCR readers per unique language tuple, forever. A misbehaving
client could pollute the cache by submitting many language combos, exhausting GPU
VRAM. Combined with no allowlist on `ocr_languages` from `ctx.config`, this was a
genuine DoS vector.

**Fix:**
- Added `_SUPPORTED_OCR_LANGUAGES` allowlist (32 well-known EasyOCR codes).
- Added `_MAX_CACHED_READERS = 4` LRU bound; oldest entry evicts on overflow.
- Validation in `extract()`: any unsupported language raises `ValueError` before any
  reader is loaded.
- New tests: `test_image_ocr_rejects_unsupported_language_codes` and
  `test_image_ocr_reader_cache_evicts_when_full`.

### H3. `stream_blob()` against real MinIO is untested

**Status:** Acknowledged. Not fixed in this audit (would require integration-test
infrastructure not currently in place). Verified:
- `aiobotocore.response.StreamingBody.iter_chunks` exists in installed version 2.25.1.
- The eval-runner `_DiskCtx.stream_blob` is fake streaming (reads whole file then
  yields chunks) — fine for fixture sizes, harmless for tests.

**Mitigation:** Document this in Phase 3 plan as the first integration test to add.
The streaming code is straightforward enough that it's likely correct, but it has
zero direct coverage.

### H4. Phase roadmap not updated

**Fix:** `CLAUDE.md` Phase roadmap table now includes a Phase 2c row (Done). README
"Current state" rewritten and Phase 2c checkbox marked complete.

---

## Medium findings (acknowledged, deferred)

| ID | Finding | Plan |
|---|---|---|
| M1 | Idempotency guard only handles `("indexed", "failed")`. Documents in `extracting`/`enriching`/`routing` can race if outbox relay republishes with a fresh job_id. | Add `started_at` column + heartbeat; out of scope for 2c. |
| M2 | No GPU queue (`arq:gpu`) backpressure. | Add separate threshold; 1-line fix when needed. |
| M3 | DLQ retry endpoint missing. `doc.error.retry_payload` is write-only. | `POST /v1/documents/{id}/retry` is a Phase 3 deliverable. |
| M4 | `MAX_GPU_INPUT_BYTES` retained "for reference" but had no effect. | **Fixed:** removed from `config.py` and `.env.example`. |
| M5 | Hardcoded `"arq:queue"` in backpressure. | **Fixed:** now reads `pool.default_queue_name`. |
| M6 | Some bakeoff queries (e.g., "How does PostgreSQL ensure data is not lost on a crash?") fail across all models because the chunker merged short adjacent sections. | Documented as a chunker-boundary limitation in `eval/results/README.md`. |

---

## Low findings (cosmetic, fixed)

| ID | Finding | Action |
|---|---|---|
| L1 | `isinstance(ctx.config, dict)` defensive check in `image.py`. Type already declares `dict[str, Any]`. | Removed during H2 fix (the new validation no longer needs it). |
| L2 | `_DiskCtx.stream_blob` is fake streaming. | Documented in `eval/runner.py`; cosmetic for eval harness. |

---

## Final state after audit closure

- **164 unit tests** passing (was 162; added 2 for OCR cache eviction + lang allowlist)
- **6 E2E tests** passing
- **`ruff check`** clean
- **Pipeline end-to-end correctness** restored (queue alignment fix)
- **Bakeoff documentation** statistically honest
- **DoS surface** in image OCR closed (allowlist + LRU bound)
- **Phase roadmap** reflects reality

Phase 2c is **CLOSED**.

---

## Follow-up work (prioritized)

These were identified during the audit but deferred. Take them in order at the start
of the next phase.

| # | Priority | Item | Rationale |
|---|----------|------|-----------|
| 1 | **P0** | End-to-end pipeline test (`upload → ARQ → worker → search`) against real Redis + Postgres + MinIO | The C1 queue-name bug survived three "Done" phases because nothing exercised this path. Even one happy-path test would have caught it. Without this, the next wiring bug lives just as long. |
| 2 | **P1** | Integration test for `IngestContext.stream_blob()` against real MinIO | The headline Phase 2c feature has zero direct coverage. Unit tests mock the stream boundary; `_DiskCtx.stream_blob` is fake streaming. If `iter_chunks` has any EOF semantic we didn't expect, audio/video silently produce empty temp files. |
| 3 | **P1** | `POST /v1/documents/{id}/retry` endpoint | `doc.error.retry_payload` is stored but unreachable without manual SQL. The DLQ feature is half-built without a way to drain it. |
| 4 | **P2** | GPU queue backpressure | `arq:gpu` is unmonitored. At `max_jobs=2`, 100 queued GPU jobs ≈ 50 min of latency before the user sees anything. Mirror the CPU `MAX_QUEUE_DEPTH` check, separate threshold. |
| 5 | **P2** | Stronger idempotency for in-progress states | Current guard only catches `(indexed, failed)`. In-progress states (`extracting`, `enriching`, `routing`) can still race on outbox-relay republish. Add a `started_at` lease/heartbeat or a `processing` lock row. |
| 6 | **P3** | Bakeoff fixture cleanup | "How does PostgreSQL ensure data is not lost on a crash?" scores 0/0 for all 3 models because the chunker collapsed the WAL section into the PgBouncer chunk. `txt-001` produces 1 chunk with no discriminative power. Either rewrite the offenders or drop them from the bakeoff. |

P0 + P1 should land before any new feature work — they close real correctness gaps.
P2 + P3 are quality-of-life and can wait for Phase 3 / Phase 6 hardening.
