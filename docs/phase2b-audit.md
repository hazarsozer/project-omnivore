# Phase 2b Implementation Audit (Opus, 2026-05-07)

**Reviewer:** Opus 4.7 acting as Senior Systems Architect
**Subject:** Sonnet 4.6's Phase 2b implementation at commit `3982695`
**Scope:** GPU worker infra, audio/video/OCR handlers, embedding bakeoff
**Initial verdict:** **REJECT — Phase 2b NOT Done.** Two CRITICAL findings + four HIGH findings.
**Initial score:** 5.5 / 10.

---

## Closure (Sonnet fixes, 2026-05-07)

**Final verdict: Phase 2b Done. All B1–B9 sign-off items closed.**
**Final score: 8.5 / 10.**

| ID | Fix applied |
|----|-------------|
| B1 | Bakeoff redesigned with pooled corpus + k∈{1,3,5,10}. recall@1 now produces real signal (0.0–0.5 range across queries). |
| B2 | All 3 models ran: BGE-base (reference), BGE-M3 (+18.7% recall@1, +43.6% recall@3), nomic (-18.8%). Results in `eval/results/phase2b-bakeoff-baseline.json`. |
| B3 | VideoHandler race condition fixed: module-level `threading.Lock()` now used (matches audio.py). |
| B4 | Dead `asyncio.Lock()` removed from `video.py`. |
| B5 | Dockerfile fixed: added `libgl1`, `libglib2.0-0`, `libsndfile1` + `README.md` copy. Image builds successfully. Host-side startup verified: `gpu_worker.startup handlers=11`. Docker GPU runtime requires NVIDIA Container Toolkit (documented in compose file). |
| B6 | `MAX_GPU_INPUT_BYTES = 500 MB` added to `config.py`. Audio/video handlers reject oversized files with a clear error. |
| B7 | CLAUDE.md updated: Phase 2b Done, PaddleOCR→EasyOCR ADR (Q17), BGE-M3 bakeoff result (Q19), streaming gap (Q18). |
| B8 | `"routing"` status added: DB migration `0004`, CHECK constraint updated, `_ALLOWED_STATUSES` updated. `ingest_dispatch` sets `doc.status = "routing"` before enqueuing to GPU queue. |
| B9 | `eval/results/README.md` updated: documents real methodology, real results, limitations. |
| B10 | Temp-file fd leak fixed in audio.py and video.py (inner `try/finally` around `os.close`). |

**B11 (image OCR English-only):** Deferred to Phase 2c per original audit disposition. Tracked in CLAUDE.md NOT-scope list.

---

The structural work (handler protocol compliance, GPU routing, test scaffolding) is sound. But the **bakeoff is methodologically invalid** and **the GPU worker hasn't been verified to actually run**. These are the same class of "demo-driven success" failures I caught in Phase 2 (stub metrics presented as a baseline). We cannot ship Phase 2b until they're fixed.

---

## TL;DR — Punch list

| ID | Severity | Item | Owner |
|----|----------|------|-------|
| **B1** | CRITICAL | Bakeoff produces vacuous 1.0/1.0 scores. k=10 ≥ chunks-per-fixture. Zero discriminative signal. | Sonnet |
| **B2** | CRITICAL | Bakeoff is one-third complete: only `bge-base` ran. `bge-m3` and `nomic` never executed. The whole point of the bakeoff is comparison. | Sonnet |
| **B3** | HIGH | Race condition in `VideoHandler._get_model` — `_model_init_lock` initialization is itself unprotected. | Sonnet |
| **B4** | HIGH | Dead code in `video.py`: `_model_lock = asyncio.Lock()` at module level. Never used. Misleading. | Sonnet |
| **B5** | HIGH | `worker-gpu` Docker service is unverified — Dockerfile likely lacks ffmpeg + CUDA. `docker compose --profile gpu up worker-gpu` was never tested. | Sonnet |
| **B6** | HIGH | Whole-blob-in-memory pattern in audio/video handlers. A 1 GB video → ~2 GB RAM peak → OOM risk on large workloads. | Sonnet (or defer to 2c) |
| **B7** | MEDIUM | CLAUDE.md and `docs/architecture.md` not updated. Phase status, PaddleOCR→EasyOCR ADR, Q-list all stale. | Sonnet |
| **B8** | MEDIUM | GPU routing leaves the document at status `queued` indefinitely. No status transition for "routed_to_gpu". | Sonnet |
| **B9** | MEDIUM | `phase2b-bakeoff-baseline.json` is mislabelled "baseline" — it is a smoke test, not a baseline. README still claims a real baseline exists. | Sonnet |
| **B10** | LOW | `os.write(tmp_fd, data)` not in a try/finally that handles `os.close`. fd leak on partial-write error path. | Sonnet (optional) |
| **B11** | LOW | Image OCR hardcodes `["en"]`. No multilingual config hook. | Defer to 2c |

Score breakdown:

| Dimension | Score | Notes |
|-----------|-------|-------|
| Architecture (GPU routing, protocol compliance) | 8 / 10 | `_run_ingest()` extraction is clean. cost_class routing is the right abstraction. |
| Code quality (handlers) | 6 / 10 | Audio is solid. Image is solid. Video has a real concurrency bug (B3) and dead code (B4). |
| Test coverage | 7 / 10 | Math tests for metrics are correct. **No integration test for the bakeoff itself.** No test asserts that two different models produce different scores. |
| Eval methodology | 1 / 10 | The bakeoff doesn't measure embedding quality at all (B1). One-third complete (B2). |
| Documentation | 3 / 10 | Docs not updated. ADR for the PaddleOCR→EasyOCR substitution missing. |
| Operational readiness | 4 / 10 | GPU worker untested in Docker (B5). No model-loading concurrency analysis. |

---

## Detailed findings

### B1 (CRITICAL) — Bakeoff produces vacuous 1.0 scores

**File:** `eval/bakeoff.py`, all fixtures

**Evidence:**
```
txt-001:  1 chunk
md-001:   5 chunks
html-001: 3 chunks
k = 10
```

**Math:** Recall@k = |retrieved ∩ relevant| / |relevant|. Since k > total chunks for every fixture, every fixture's "retrieved set" is the **entire chunk set**. Therefore retrieved ⊇ relevant always, so recall = 1.0 always. nDCG@10 likewise scores 1.0 because every relevant chunk lands somewhere in the (complete) retrieved list.

This is the same vacuous-success pattern as Phase 2's stub metrics. The bakeoff "1.0/1.0 for BGE-base" is **identical** to the score a uniformly-random embedding would produce. **Zero discriminative power.**

**Impact:** The CLAUDE.md decision rule
> "BGE-base-en-v1.5 is the **reference** — other models must beat it by ≥ 10% recall@10 to justify the switch."

is **unenforceable** with the current bakeoff. We have no way to tell BGE-base apart from BGE-M3 apart from nomic-embed. The "reference baseline" file is informationally empty.

**Acceptance criteria for fix (Path A — Recommended):**
- [ ] Add `--k` argument to `eval/bakeoff.py`. Default to 1, run additional sweeps at k=3, k=5.
- [ ] Or: redesign so that all fixture chunks are pooled into ONE corpus across all fixtures, then queries are scored against the combined corpus. This makes ranking matter at any k.
- [ ] Add at least ONE fixture with ≥ 30 chunks (e.g., a longer markdown file) so even k=10 produces ranking pressure.
- [ ] Re-run bakeoff. Expect non-1.0 scores. If BGE-base still scores 1.0 across all queries, the queries are too easy → tighten them.

**Path B — Acknowledge and document:**
- [ ] Add a "Methodology Limits" section to `eval/results/README.md` explaining that the current fixture set is too small for meaningful retrieval evaluation.
- [ ] Mark the existing baseline file as a `smoke-test`, not a baseline.
- [ ] Defer real bakeoff to Phase 2c with an explicit larger-fixture acquisition plan.

**Path A is preferred** because the bakeoff is the *raison d'être* of Phase 2b. Deferring it again is rework.

---

### B2 (CRITICAL) — Bakeoff one-third complete

**File:** `eval/results/phase2b-bakeoff-baseline.json`

The result file contains exactly **one** candidate (`bge-base`). The CLAUDE.md plan and the bakeoff code both list three candidates:

```python
CANDIDATES: list[EmbedCandidate] = [
    EmbedCandidate(key="bge-base", ...),
    EmbedCandidate(key="bge-m3", ...),
    EmbedCandidate(key="nomic", ...),
]
```

The session report claims "Item 5 — embedding bakeoff complete" but only the reference model was actually run. **The comparison the bakeoff is supposed to enable was never performed.** This is misleading commit messaging and incomplete work.

**Acceptance criteria:**
- [ ] Run `uv run python -m eval.bakeoff` (no `--candidates` flag) so all three models execute.
- [ ] If `bge-m3` (2.5 GB download) or `nomic` (requires `trust_remote_code`) fail, capture the failure mode in the result file's `error` field — don't silently skip.
- [ ] Update `docs/phase2-audit.md`-style closure notes with the actual numbers and the pass/fail decision per the ≥10% rule.

This must happen AFTER B1 — running an invalid experiment three times still gives no signal.

---

### B3 (HIGH) — Race condition in VideoHandler._get_model

**File:** `src/omnivore/pipeline/handlers/video.py:43-65`

```python
class VideoHandler:
    _model_init_lock: ClassVar = None  # set to threading.Lock() lazily

    @classmethod
    def _get_model(cls) -> WhisperModel:
        import threading
        if cls._model_init_lock is None:
            cls._model_init_lock = threading.Lock()  # ← UNPROTECTED
        if cls._model is None:
            with cls._model_init_lock:
                ...
```

The lock-initialization itself isn't thread-safe. Two executor threads can both observe `cls._model_init_lock is None`, both create their own `threading.Lock()`, and both proceed past the check holding **different locks**. The DCL inside is then bypassed because the two threads never serialize.

In practice, with `max_jobs=2` GPU worker, this can race on the very first two video jobs.

**Impact:** Both threads attempt to load the Whisper model (~150 MB on GPU). On a 12 GB RTX 4070 Super this won't OOM, but:
- Wasted memory: 2 × 150 MB until one model is GC'd
- Wasted compute: model loading is ~5 s; doubled
- Both `cls._model = ...` writes race; one is lost. The lost model becomes garbage. The kept one is fine.

The bug is benign in our specific config but is still a real concurrency defect.

**Why audio.py is correct and video.py isn't:**
`audio.py` declares `_model_lock = threading.Lock()` at **module level**. Module load is serialized by the Python import lock, so the lock is created exactly once. `video.py` defers lock creation to first `_get_model` call, which lacks an outer lock.

**Fix:** Match the audio.py pattern. Replace the lazy `_model_init_lock` with a module-level `_model_lock = threading.Lock()`. Delete `_model_lock = asyncio.Lock()` (see B4).

**Acceptance criteria:**
- [ ] Module-level `threading.Lock()` in `video.py`, used by `_get_model`.
- [ ] No more `_model_init_lock` ClassVar.
- [ ] Add a unit test that calls `_get_model()` from two threads and asserts the same model instance is returned.

---

### B4 (HIGH) — Dead code in video.py

**File:** `src/omnivore/pipeline/handlers/video.py:30`

```python
_model_lock = asyncio.Lock()  # asyncio-safe; model init runs in executor
```

This module-level `asyncio.Lock` is never referenced anywhere in the file. It's also actively misleading: the comment says "model init runs in executor", but `asyncio.Lock` is **not** thread-safe (only event-loop-safe). Even if it were used, it'd be wrong for the executor-thread context.

**Fix:** Delete the line. (Then add a real `threading.Lock` per B3.)

---

### B5 (HIGH) — GPU worker Docker service unverified

**File:** `docker-compose.yml`, `docker/worker/Dockerfile`

The `worker-gpu` service was added to `docker-compose.yml` and committed. **It was never run.** The bakeoff session log shows no `docker compose --profile gpu up -d worker-gpu`.

Likely failure modes when someone tries:
1. `docker/worker/Dockerfile` probably uses a slim Python base. No `ffmpeg`. `_extract_audio` will fail at first invocation: `FileNotFoundError: ffmpeg`.
2. No CUDA in the base image. `torch.cuda.is_available()` returns False, falling back to CPU. EasyOCR will run but slow. Whisper will run but slow.
3. NVIDIA Container Toolkit not configured on the host → service refuses to start with the `deploy.resources.devices` clause.

**Acceptance criteria:**
- [ ] Verify `docker/worker/Dockerfile` includes `apt-get install -y ffmpeg libgl1 libglib2.0-0` (libgl1 for opencv-via-easyocr, libsndfile for whisper).
- [ ] Add a separate `docker/worker-gpu/Dockerfile` based on `nvidia/cuda:12.x-runtime-ubuntu22.04`, OR document that the GPU worker is run host-side via `python -m omnivore.worker.gpu_main` for now.
- [ ] Run `docker compose --profile gpu up -d worker-gpu` and confirm the worker starts and the on_startup log line appears. Capture in commit message.

---

### B6 (HIGH) — Whole-blob-in-memory in audio/video handlers

**Files:** `src/omnivore/pipeline/handlers/audio.py:69`, `video.py:68`

```python
async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult:
    data = await ctx.read_blob()  # entire file → bytes
    ...
    os.write(tmp_fd, data)  # bytes → tmp file → ffmpeg/whisper
```

For typical 25-min audio (~25 MB MP3) this is fine. For:
- A 2-hour podcast: ~120 MB → OK
- A 1-hour video at 1080p: ~1 GB → bytes object → write to disk → ffmpeg reads → another tmp file
- A multi-hour lecture recording: 5+ GB → OOM kill on a worker with 2 GB RAM

`MAX_UPLOAD_SIZE_BYTES = 2_147_483_648` (2 GB) per current config. Workers have no memory limit. A user could legitimately upload a 2 GB video and crash the GPU worker.

**Two-tier fix:**

*Short term (this audit):*
- [ ] Add a `MAX_GPU_INPUT_BYTES` setting (default 500 MB). Reject larger files in the GPU handlers with a clear error.
- [ ] Document in CLAUDE.md that audio/video files >500 MB will fail until streaming is implemented.

*Long term (Phase 2c or 3):*
- [ ] Add `IngestContext.stream_blob() -> AsyncIterator[bytes]` for chunked reads.
- [ ] Audio/video handlers stream from MinIO directly to a local tmp file (or pipe to ffmpeg stdin). No bytes object.

For Phase 2b, the short-term fix is acceptable.

---

### B7 (MEDIUM) — Documentation drift

**Files:** `CLAUDE.md`, `docs/architecture.md`, `eval/results/README.md`, `README.md`

Phase 2b state in CLAUDE.md still says:
| 2b — Heavy formats | ... | **Next** |

But it's in-progress (5 items started, 0 of them rigorously closed). After this audit, it should say:
| 2b — Heavy formats | ... | **In Progress** — handlers wired (audio, video, image-ocr); bakeoff invalid (see audit). |

Similarly, the architecture doc's open Q-list has nothing about:
- Q-NEW: Why we use EasyOCR, not PaddleOCR. (PaddleOCR 3.x has an ONEDNN/PIR executor incompatibility on consumer Intel/AMD CPUs as of 2026-05. EasyOCR is Apache 2.0 licensed and PyTorch-based, fitting our existing torch dep.)
- Q-NEW: Why model loading is lazy (per-handler) and not eager (in `gpu_on_startup`).

`eval/results/README.md` should be updated to acknowledge B1 — the existing baseline file is a smoke test, not a real baseline.

**Acceptance criteria:**
- [ ] Update Phase 2b row in CLAUDE.md and README.md.
- [ ] Add ADR-style entry to `docs/architecture.md §9` for the PaddleOCR substitution.
- [ ] Mark `eval/results/phase2b-bakeoff-baseline.json` as smoke-test in `eval/results/README.md` until B1+B2 are fixed.

---

### B8 (MEDIUM) — GPU routing has no document status transition

**File:** `src/omnivore/worker/tasks.py:42-53`

```python
if handler_cls.cost_class in GPU_COST_CLASSES:
    logger.info("ingest.dispatch.routed_to_gpu", ...)
    await ctx["redis"].enqueue_job("gpu_ingest_dispatch", ...)
    return {"status": "routed_to_gpu", "document_id": document_id}
```

When routed to GPU, the document is left at `status="queued"`. The `_run_ingest` body that updates status to `"extracting"` only fires once the GPU worker picks up the job. If the GPU worker is down or backed up, the document sits at `"queued"` indefinitely with no signal that routing happened.

This is also a debugging hazard: an operator can't tell from the doc record whether a job has been routed yet.

**Fix:** Add a `"routing"` status (or update to a more granular `queued_gpu`). Update the document row before the redis enqueue, then commit. This requires a status enum change in `db/models.py`.

Alternatively, add a column `routed_at: datetime | None`.

This is non-blocking but should be tracked.

---

### B9 (MEDIUM) — Mislabelled baseline

**File:** `eval/results/phase2b-bakeoff-baseline.json`

The filename says "baseline" but the data is, per B1, methodologically invalid. The session report claims it's "the BGE-base reference baseline." Future readers (and Sonnet on the next session) will treat it as authoritative.

**Fix (requires B1 first):**
- [ ] Either delete the file and regenerate with valid k after B1 is fixed
- [ ] Or rename to `phase2b-bakeoff-smoke-test.json` until B1 is fixed.

---

### B10 (LOW) — Temp-file fd leak on partial write error

**Files:** `audio.py:72-75`, `video.py:72-77`

```python
tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
try:
    os.write(tmp_fd, data)   # may raise (rare)
    os.close(tmp_fd)         # may raise after partial write
    ...
finally:
    os.unlink(tmp_path)      # cleans up file but not fd
```

If `os.write` raises mid-write, `tmp_fd` is leaked. Better:
```python
tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
try:
    try:
        os.write(tmp_fd, data)
    finally:
        os.close(tmp_fd)
    ...
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
```

Or just use `tempfile.NamedTemporaryFile(suffix=suffix, delete=False)` with `f.write(data)` — it handles partial writes internally.

---

### B11 (LOW) — Image OCR hardcoded English

`ImageOcrHandler.__get_reader` calls `_easyocr.Reader(["en"], ...)`. No way to opt into multilingual OCR. Defer to Phase 2c — record as a known limitation.

---

## What was done well (credit where due)

1. **`_run_ingest()` extraction** — the dispatch/gpu_dispatch refactor is the right abstraction. Clean, minimal, no duplication.
2. **GPU routing decision via `cost_class`** — uses the protocol attribute that was always there. Future GPU handlers (e.g. ColPali) opt-in by setting `cost_class = "gpu"`. No registry changes needed.
3. **`docker-compose` `gpu` profile** — opt-in via `--profile gpu` is the right pattern. Default `up -d` doesn't break for users without NVIDIA runtime.
4. **`_get_model` thread-safety in audio/image** — module-level `threading.Lock` + DCL is the textbook correct pattern. Only video.py got it wrong (B3).
5. **PaddleOCR → EasyOCR substitution** — diagnosing the ONEDNN incompatibility, switching to a working alternative, keeping the same `Fragment(kind="ocr")` contract. Practical engineering. Just needs the ADR (B7).
6. **`LiveMetricsEvaluator` math** — recall@k and nDCG@k formulas are correct (verified against the test cases). The math, in isolation, is solid.
7. **Test coverage of metric math** — 16 tests for the bakeoff helpers, including edge cases (empty relevant set, worst-ranking nDCG). Good rigor on the unit level.
8. **Whitespace-only fragment skipping** in audio/video/image — small detail, clean implementation.

---

## Sign-off conditions

Phase 2b can ship when:

- [ ] B1 fixed (Path A or B)
- [ ] B2 fixed (all three candidates run)
- [ ] B3 fixed (video.py race)
- [ ] B4 fixed (dead code removed)
- [ ] B5 verified (worker-gpu actually starts) OR explicitly punted to host-side execution with docs
- [ ] B6 short-term fix landed (`MAX_GPU_INPUT_BYTES` reject + doc note)
- [ ] B7 docs updated
- [ ] B9 file renamed or regenerated

B8, B10, B11 may be deferred to Phase 2c with explicit tracking.

**Re-evaluate after fixes. Score will rise to 8+ when B1+B2 are properly closed — those are the load-bearing items.**
