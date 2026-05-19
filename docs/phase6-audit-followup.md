# Phase 6 Audit Follow-up — 2026-05-19

**Reviewer:** Opus 4.7 (Senior Systems Architect)
**Subject:** Sonnet 4.6's fix pass in commit `077a8d2`, addressing the 9 issues raised in [`docs/phase6-audit.md`](phase6-audit.md).
**Verdict:** **PASS** — all 9 fixes correctly applied, 396 tests passing, ruff clean, runtime behaviour verified. Phase 6 closes.

---

## Fix-by-fix verification

### C-1 ✅ — Three M-fixes correctly re-applied to `api/main.py`

**M-3 (queue depth warmup):** Line 102–104:
```python
# M-3: pre-initialise QUEUE_DEPTH time series so Grafana never shows "no data"
QUEUE_DEPTH.labels(queue="default").set(0)
QUEUE_DEPTH.labels(queue="gpu").set(0)
```
Correctly placed AFTER `setup_tracing(settings)` (so tracing context exists) and BEFORE `registry.discover()`. Both queue labels are initialised; Grafana panels will show 0 instead of "no data" on fresh start.

**M-5 (BatchSpanProcessor shutdown):** Line 124–127:
```python
# M-5: flush pending spans before the process exits
provider = trace.get_tracer_provider()
if hasattr(provider, "shutdown"):
    provider.shutdown()
```
Correctly placed AFTER `arq_pool.close()` and BEFORE the shutdown log line. The `hasattr` guard handles the NoOp provider case (when OTel is disabled). In-flight spans now flush on graceful shutdown.

**M-6 (FastAPIInstrumentor gate):** Line 146–149:
```python
# OTel FastAPI auto-instrumentation — adds http.server spans for every request.
# M-6: only instrument when OTEL_ENABLED=True to avoid silent error swallowing.
if get_settings().OTEL_ENABLED:
    FastAPIInstrumentor.instrument_app(app)
```
The bare `try/except: pass` is gone. Errors now propagate at module-load time. Combined with M-4 (`OTEL_ENABLED=False` default), this means instrumentation is opt-in.

---

### C-2 ✅ — Multi-line PEM documentation fixed + startup validation added

**Documentation fixes:**
- README §2 (lines 56–67) now shows the quoted form explicitly with a concrete example.
- `.env.example` (lines 21–35) shows commented examples of the quoted-PEM pattern.
- Both replace the previous misleading "multi-line is fine" claim.

**Startup validation:** `api/main.py` lines 82–97:
```python
# C-2: Validate JWT keys are not truncated (unquoted multi-line PEM in .env
# loads only the BEGIN header — 27 chars — breaking the token exchange endpoint).
_priv = settings.JWT_PRIVATE_KEY_PEM.get_secret_value()
_pub = settings.JWT_PUBLIC_KEY_PEM
if _priv and len(_priv) < 200:
    raise RuntimeError(
        "JWT_PRIVATE_KEY_PEM appears truncated (only "
        f"{len(_priv)} chars). Wrap the full PEM in double quotes in .env — "
        "see README §2 'Generate the JWT keypair'."
    )
if _pub and len(_pub) < 100:
    raise RuntimeError(...)
```

The thresholds are well-chosen: a real 2048-bit PKCS8 private key is ~1700 chars (200 is generous and catches the 27-char BEGIN-header-only case); a 2048-bit public key is ~451 chars (100 is generous).

**The `_priv and len(_priv) < 200` predicate correctly skips the empty-key case** — users who haven't configured JWT auth at all aren't penalised; `auth_route.py::exchange_token` returns 501 in that case.

**Verified at runtime:**
```python
$ uv run python -c "...lifespan with truncated key..."
PASS: lifespan raises RuntimeError: JWT_PRIVATE_KEY_PEM appears truncated (only 27 chars). Wrap...
```

A truncated key now produces a clear startup error pointing at the README section, instead of a generic 500 on the first `/v1/auth/token` call.

---

### C-3 ✅ — HNSW index try/except removed from migration 0011

Lines 142–153:
```python
# HNSW index — partial, matching the definition from migration 0009.
# Requires the pgvector extension to be loaded. This project's Docker image
# (pgvector/pgvector:pg16) includes it by default; if you see an error here,
# verify that the pgvector extension is installed in your PostgreSQL instance.
op.execute(
    """
    CREATE INDEX idx_chunks_embedding ON core.chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE embedding IS NOT NULL AND 'vector' = ANY(sinks)
    """
)
```

The `try: ... except Exception: pass` block is gone. A missing pgvector extension now fails the migration loudly with the Postgres error, instead of "succeeding" with no vector index. The replacement comment correctly directs operators to verify the extension if the error fires.

---

### H-1 ✅ — Partition script rewritten to use asyncpg

`scripts/create_monthly_partition.py`:

- `asyncpg_url()` now correctly handles both `postgresql://` (legacy) and `postgresql+asyncpg://` (canonical) DSNs.
- `_run_sql()` and `_check_exists()` are async functions using `create_async_engine` from `sqlalchemy.ext.asyncio`. Both correctly `engine.dispose()` in a `finally` clause.
- `execute_sql()` and `partition_exists()` are sync wrappers using `asyncio.run()`.
- No `psycopg2` import anywhere.

**Verified at runtime:**
```python
$ uv run python -c "from scripts.create_monthly_partition import build_sql, partition_name, ..."
build_sql: CREATE TABLE IF NOT EXISTS core.jobs_2026_07
    PARTITION O...
partition_name(2026, 12): jobs_2026_12
next_month(Dec 2026): (2027, 1)
partition_bounds(2027, 2): ('2027-02-01', '2027-03-01')
```

Logic works correctly across year boundaries. The dry-run mode also works without touching the DB.

**Minor observation (not a blocker):** Each `execute_sql()` / `partition_exists()` call creates and disposes a fresh engine. For a CLI script this is fine; for a long-running service it would be inefficient. Acceptable as-is.

---

### H-2 ✅ — CI filter dropped

`.github/workflows/ci.yml` line 123:
```yaml
run: uv run pytest tests/ -q
```

The `-m "not integration"` filter is gone. All 396 tests will now run in CI. The pgvector + redis + minio service containers were already in place and will now actually exercise the integration test suite.

---

### H-3 ✅ — DELETE handler cleans up unpublished outbox entries

`src/omnivore/api/routes/documents.py` lines 333–345:
```python
# Outbox has no FK to documents (by design — aggregate_id is untyped).
# Clean up any unpublished outbox entries to prevent the outbox_relay from
# trying to re-enqueue a job for a document that no longer exists.
await db.execute(
    sa_delete(Outbox).where(
        Outbox.aggregate_id == document_id,
        Outbox.published_at.is_(None),
    )
)

# FK CASCADE handles chunks, entities, extracted_tables, extracted_rows, jobs.
await db.delete(doc)
await db.commit()
```

Implementation notes:
- Uses `sqlalchemy.delete` (aliased as `sa_delete` to avoid shadowing the `delete` route name).
- Filters to only **unpublished** outbox rows — published rows are preserved as audit trail.
- The outbox cleanup is in the same transaction as the document delete — atomic.
- The comment now correctly states the FK CASCADE reach and explicitly notes outbox is intentional design.

**Subtle correctness check:** The outbox table has no RLS (verified — not in `_TENANT_TABLES` of migration 0008), so the `tenant_session` GUC doesn't restrict the DELETE. The query filters by `aggregate_id == document_id` only — which is safe because document IDs are tenant-scoped UUIDs, and the doc ownership check (line 305) already verified tenant ownership. ✅

---

### M-2 ✅ — `ADMIN_BOOTSTRAP_TOKEN` default aligned

- `src/omnivore/config.py` line 29: `ADMIN_BOOTSTRAP_TOKEN: SecretStr = SecretStr("change-me-before-first-run")`
- `.env.example` line 36 (untouched, was already `change-me-before-first-run`)
- `README.md` line 226: `| change-me-before-first-run | Root admin token — change before first run |`

All three sources now agree. ✅

---

### M-3 ✅ — Grafana remapped to port 3001

- `docker-compose.yml` line 184: `"3001:3000"` with explanatory comment.
- `README.md` line 149: Grafana URL now `http://localhost:3001`.
- `README.md` line 167 (service table): Grafana port `3001 (monitoring profile)`.

No port conflict with the Next.js frontend dev server. ✅

---

### M-4 ✅ — `OTEL_ENABLED` default flipped to false

- `src/omnivore/config.py` line 48: `OTEL_ENABLED: bool = False`.
- `.env.example` line 65–68: default `OTEL_ENABLED=false` with clear comment.
- `README.md` line 236: `| OTEL_ENABLED | false | Enable OpenTelemetry tracing (set true with --profile monitoring) |`.

The OTLP exporter no longer fires by default, eliminating the connection-warning noise on fresh installs without the monitoring profile. ✅

---

## Cross-cutting verification

### Test suite

```
$ ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q
396 passed, 2 warnings in 8.30s
```

No regressions. The `M-2` change to `ADMIN_BOOTSTRAP_TOKEN`'s default could have broken tests that relied on the prior `change-me-admin-token` value — it did not, because all tests use the explicit `test-admin-bootstrap-for-integration-only` override via env var.

### Static analysis

```
$ uv run ruff check src/ tests/ scripts/
All checks passed!
```

Import ordering issues introduced by the new imports (`from sqlalchemy import delete as sa_delete` in documents.py, `import asyncio` in the partition script) were auto-fixed before commit.

### Behavioural smoke tests

Three runtime verifications passed:
1. C-2: API lifespan raises `RuntimeError` on a 27-char (truncated) `JWT_PRIVATE_KEY_PEM` with a clear, actionable message.
2. C-3: Migration 0011 file no longer contains any `try/except` around the HNSW index creation.
3. H-1: Partition script's logic (build_sql, partition_name, next_month, partition_bounds) works correctly including across the December→January year boundary.

---

## Observations (non-blocking)

These are notes for v1.1 polish, not regression blockers:

1. **No new regression tests for the C-1 / C-2 fixes.** The audit suggested adding three tests to prevent these from re-regressing (one each for M-3, M-5, M-6 + one for the C-2 length check). Sonnet skipped these. The fixes are correct in the current code, but if someone else does another worktree-style refactor of `api/main.py`, the same C-1 regression could recur. Recommend adding the suggested tests as a follow-up.

2. **C-2 length check is necessary but not sufficient.** A user who pastes the base64 body of a PEM (without the BEGIN/END headers) could produce a 200+ char "key" that passes the length check but fails at signing time. A stricter check like `_priv.startswith("-----BEGIN") and "-----END" in _priv` would close this gap. Not urgent because the README now shows the right format.

3. **Partition script creates two separate event loops per invocation** (one for `partition_exists`, one for `execute_sql`). Wasteful but correct for a CLI script. Could be coalesced into a single async main() function as a minor optimization.

4. **No update to `docs/architecture.md`** noting the destructive nature of migrations 0011/0012 on populated databases. The README "Roadmap" line for Phase 6 still says "DB partitioning groundwork" without flagging the maintenance-window requirement. Consider adding a callout in `docs/architecture.md §9` (open questions) or a dedicated migration runbook.

5. **`_seen_tenant_ids` cardinality cap (M-1 from the original audit)** still has no `threading.Lock`. Acceptable in the all-asyncio code paths used today; would need attention if any sync thread-pool work is added. Deferred from the original audit; remains deferred.

None of these block Phase 6 closure.

---

## Final verdict

**Phase 6 audit: CLOSED.**

All 3 CRITICAL and all 3 HIGH issues are correctly resolved. 3 of the 6 MEDIUM issues are resolved (M-2, M-3, M-4); the remaining 3 mediums (M-1 cardinality lock, M-5 cost dashboard UUID legend, M-6 partitioning maintenance docs) were correctly deferred to v1.1 polish. All 5 LOW issues remain open as documented.

The project is ready to ship as the v1 open-source release once:
- `.env` is generated from `.env.example` with the documented quoted-PEM format
- A real keypair and admin bootstrap token are generated
- `alembic upgrade head` runs against a pgvector-enabled Postgres instance

No further architect intervention required.

— Opus 4.7, Senior Systems Architect
