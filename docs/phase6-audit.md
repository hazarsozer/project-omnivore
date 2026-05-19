# Phase 6 + Open-Source Readiness Audit — 2026-05-19

**Reviewer:** Opus 4.7 (Senior Systems Architect)
**Subject:** Sonnet 4.6's two Phase 6 commits — `2ac7290` (observability hardening, chaos tests, DB partitioning, frontend JWT auth) and `9c8140a` (README + DELETE endpoint + BM25 sinks + CORS + CI + partition script).
**Verdict:** **PASS WITH BLOCKING FIXES** — 3 CRITICAL + 3 HIGH + 6 MEDIUM + 5 LOW issues. The scope was ambitious and most of it landed correctly. But three failures matter:

1. The Phase 5 follow-up fixes for `api/main.py` (M-3, M-5, M-6) were **silently regressed** during the second commit's worktree merge — they were present in `2ac7290` and removed in `9c8140a`.
2. The README and `.env.example` claim that multi-line PEM keys work without quoting. **They do not.** A user following the documented quickstart ends up with a one-line key, a broken JWT flow, and a generic 500 error from `/v1/auth/token`.
3. Migration 0011 silently swallows HNSW index creation failures, so a missing pgvector extension produces a "successful" migration with no vector index and catastrophic search latency.

The rest is well-structured. 396 tests pass with infra running, ruff is clean, the chaos tests do exercise real failure modes, the partitioning migrations are well-reasoned, and the frontend auth flow is sensibly designed. Once the three CRITICAL issues are closed, Phase 6 is shippable as the v1 open-source release.

---

## Scope reviewed

**Commit `2ac7290` — Phase 6:**
- `src/omnivore/observability.py` — `_cap_tenant_id()`, `STORAGE_BYTES_TOTAL`, noop `setup_metrics()`
- `src/omnivore/logging_config.py` — M-4 `ctx.is_valid` fix
- `src/omnivore/api/main.py` — claimed M-3 / M-5 / M-6 fixes
- `src/omnivore/worker/tasks.py` — L-7 tracer name, M-1 cap usage, M-5 shutdown, STORAGE_BYTES emit
- `src/omnivore/worker/gpu_main.py` — inherits `on_shutdown` from tasks.py
- `observability/promtail-config.yaml`, `observability/grafana/provisioning/datasources/datasources.yaml`, `observability/grafana/dashboards/cost.json`, `docker-compose.yml` (OTEL comment), `.env.example` (initial pass)
- `alembic/versions/0010_add_jobs_created_at.py`, `0011_partition_chunks.py`, `0012_partition_jobs.py`
- `src/omnivore/db/models.py` — `Job.created_at` composite PK
- `tests/chaos/*` — 29 tests
- `frontend/src/lib/auth.ts`, `frontend/src/app/login/page.tsx`, `frontend/src/components/AuthGuard.tsx`, `frontend/src/lib/api.ts`, `frontend/src/app/layout.tsx`, `frontend/src/components/sidebar-nav.tsx`

**Commit `9c8140a` — Open-source readiness:**
- `README.md` (rewrite), `CONTRIBUTING.md` (new), `.env.example` (auth section)
- `src/omnivore/api/routes/documents.py` — DELETE endpoint
- `src/omnivore/api/routes/search.py` — BM25/vector/hybrid sinks filter
- `src/omnivore/api/main.py` — `ALLOWED_ORIGINS` thread-through
- `src/omnivore/config.py` — `ALLOWED_ORIGINS` setting
- `.github/workflows/ci.yml` (new), `scripts/create_monthly_partition.py` (new), `scripts/README.md` (new)
- `tests/unit/test_routes_documents.py` (delete tests), `tests/unit/test_routes_search.py` (sinks tests)

The scope is correct; the surface is large. The audit below focuses on what is broken or misleading, not on what was built well.

---

## CRITICAL — must fix before Phase 6 closes

### C-1: Three Phase 5 follow-up fixes were regressed by the second commit

**Where:** `src/omnivore/api/main.py` at `9c8140a` vs `2ac7290`.

**What was claimed (in `2ac7290`):** M-3 (QUEUE_DEPTH warmup), M-5 (BatchSpanProcessor.shutdown), M-6 (FastAPIInstrumentor gated on OTEL_ENABLED).

**What is in `main` now:** None of the three.

**Reproduction:**
```bash
$ git show 2ac7290:src/omnivore/api/main.py | grep -c "QUEUE_DEPTH.labels(queue=\"default\").set(0)\|provider.shutdown\|OTEL_ENABLED:"
3
$ git show 9c8140a:src/omnivore/api/main.py | grep -c "QUEUE_DEPTH.labels(queue=\"default\").set(0)\|provider.shutdown\|OTEL_ENABLED:"
0
```

The current `lifespan()` (lines 76–102 of `api/main.py`):
- Calls `setup_tracing(settings)` but never pre-initialises `QUEUE_DEPTH.labels(queue="default"|"gpu").set(0)` (M-3 missing)
- After `yield`, cancels the poller, closes Redis and ARQ — but never calls `provider.shutdown()` on the OTel tracer provider (M-5 missing). In-flight spans queued by `BatchSpanProcessor` are lost on graceful shutdown.
- Module-level `try: FastAPIInstrumentor.instrument_app(app); except Exception: pass` is unchanged (M-6 missing). If instrumentation fails (version skew, attribute conflict), every `http.server` span disappears silently and there's no log trail.

**Root cause:** Agent A (observability) and Agent 2 (DELETE/CORS) both modified `src/omnivore/api/main.py` from separate worktrees, both rooted at commit `f712cb7`. Sonnet's merge step copied Agent A's `main.py` into main in the first commit, then in the second commit copied Agent 2's `main.py` over the top — overwriting Agent A's M-3/M-5/M-6 changes because Agent 2's worktree did not contain them. The diff between the two commits shows the regression cleanly.

**Impact:**
- **M-3:** Fresh API start with empty queues → Grafana's "Queue Depth" panel shows "no data" for up to 30 s until the first `_poll_queue_depth` tick fires. Minor UX issue.
- **M-5:** API process shutdown (Ctrl-C, SIGTERM from supervisor) loses any spans the `BatchSpanProcessor` had buffered but not yet exported. Symptom: traces near a deploy are incomplete.
- **M-6:** If `opentelemetry-instrumentation-fastapi` is incompatible with the installed FastAPI version (which has happened repeatedly across releases), every HTTP span vanishes and there is no log to investigate. Phase 5's audit explicitly called this out as the worst kind of silent failure.

**Fix:** Re-apply the three fixes in `api/main.py`. The full patch is small and is exactly what was in `2ac7290`. The worktree at `.claude/worktrees/agent-a349872350ec818f4` still contains the correct version — diffing against the current `main.py` produces the exact patch to apply:

```python
# Add to imports
from opentelemetry import trace

# In lifespan(), after `setup_tracing(settings)` and before registry.discover():
QUEUE_DEPTH.labels(queue="default").set(0)
QUEUE_DEPTH.labels(queue="gpu").set(0)

# In lifespan(), after `await app.state.arq_pool.close()` and before `logger.info("shutdown")`:
provider = trace.get_tracer_provider()
if hasattr(provider, "shutdown"):
    provider.shutdown()

# Replace the try/except around FastAPIInstrumentor with a guard:
if get_settings().OTEL_ENABLED:
    FastAPIInstrumentor.instrument_app(app)
```

Note that `get_settings()` is already called on line 113 for `ALLOWED_ORIGINS`, so the M-6 guard is consistent with existing code patterns.

---

### C-2: The documented multi-line PEM approach silently breaks the JWT flow

**Where:** `README.md` lines 56–58 ("Multi-line PEM is fine — pydantic-settings reads it correctly"), `.env.example` line 26 (same claim).

**Reproduction:**
```bash
# Generate a real keypair
$ openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /tmp/p.pem
$ openssl rsa -pubout -in /tmp/p.pem -out /tmp/pub.pem

# Follow the README — paste the PEM verbatim into .env (no quoting)
$ cat /tmp/p.pem >> .env  # mimicking the README's "paste the full PEM content"
# (in practice prepend `JWT_PRIVATE_KEY_PEM=` to the first line)

# Start the API — observe python-dotenv warnings
$ uv run uvicorn omnivore.api.main:app
python-dotenv could not parse statement starting at line 54
python-dotenv could not parse statement starting at line 63
...

# What pydantic-settings actually loaded
$ uv run python -c "from omnivore.config import get_settings; \
  print(len(get_settings().JWT_PRIVATE_KEY_PEM.get_secret_value()))"
27   # ← only the length of "-----BEGIN PRIVATE KEY-----"

# What the JWT flow does in this state
$ curl -s -X POST http://localhost:8000/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"api_key": "omn_live_..."}' | python3 -m json.tool
{"success": false, "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}}
```

I tested this on the running session and confirmed: `/v1/auth/token` returns 500 because `pyjwt.encode(payload, "<27-char string>", algorithm="RS256")` raises in the global exception handler.

**Why the API "appeared" to work in our session:** every prior test used `X-API-Key` directly, which never invokes the JWT signing path. The frontend's auth flow (login page → `/v1/auth/token`) was never exercised end-to-end. A user who follows the README to set up auth and tries to log into the frontend will hit a generic 500.

**Why pydantic-settings doesn't reject the value:** the field is `SecretStr | None = None` with default `""`. python-dotenv parses each line independently; the first line assigns `JWT_PRIVATE_KEY_PEM` to the BEGIN header string and the subsequent lines are ignored with warnings. pydantic-settings receives a non-empty value and considers the field set.

**Quoted multi-line works:** I verified this directly:
```bash
$ cat > /tmp/.env.test <<'EOF'
JWT_PRIVATE_KEY_PEM="-----BEGIN PRIVATE KEY-----
fakebody1
fakebody2
-----END PRIVATE KEY-----"
EOF
$ uv run python -c "..." # pydantic-settings load
Length: 73
Has body: True
```

**Fix (two parts):**

1. **Update the documentation** to instruct users to wrap the PEM in double quotes, or to base64-encode it. Two viable options:

   **Option A — double-quote wrap (least invasive):**
   ```bash
   # In .env, write JWT_PRIVATE_KEY_PEM like this:
   JWT_PRIVATE_KEY_PEM="-----BEGIN PRIVATE KEY-----
   <base64 body>
   -----END PRIVATE KEY-----"
   ```
   Update `.env.example` line 25–27 to demonstrate the quoted form. Update README lines 56–58 to match. Remove the "multi-line PEM is fine" claim — replace with "wrap the full PEM in double quotes so .env parsers preserve the line breaks".

   **Option B — base64-encode (more robust):**
   Add a small helper that decodes base64 in `config.py`:
   ```python
   import base64
   @model_validator(mode="after")
   def _decode_pem(self):
       v = self.JWT_PRIVATE_KEY_PEM.get_secret_value()
       if v and not v.startswith("-----"):
           # Assume base64-encoded PEM
           self.JWT_PRIVATE_KEY_PEM = SecretStr(base64.b64decode(v).decode())
       return self
   ```
   Then the .env file contains a single-line base64 string — no quoting concerns. Update README to recommend `openssl ... | base64 -w0` for setup.

   **Recommendation: Option A.** It is two lines of documentation; option B adds load-time logic to a security-critical path.

2. **Fail loud, not silent.** Add a startup validation in `lifespan()` that checks `len(settings.JWT_PRIVATE_KEY_PEM.get_secret_value()) > 200` and refuses to start if the PEM is suspiciously short (a real 2048-bit RSA private key in PKCS8 is ~1700 bytes; the BEGIN header alone is 27 bytes). Log a clear error: `"JWT_PRIVATE_KEY_PEM appears truncated — see README for multi-line .env quoting"`. Same for the public key.

   This converts a runtime 500 on first JWT exchange into a startup failure with a clear message. The marginal cost is one length check.

---

### C-3: Migration 0011 silently swallows HNSW index creation failure

**Where:** `alembic/versions/0011_partition_chunks.py` lines 142–157.

```python
# HNSW index — partial, matching the definition from migration 0009.
# pgvector must be loaded and the extension must exist; if it is not
# available (e.g., test environment without pgvector), we swallow the
# error so the rest of the migration succeeds.
try:
    op.execute(
        """
        CREATE INDEX idx_chunks_embedding ON core.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL AND 'vector' = ANY(sinks)
        """
    )
except Exception:
    pass
```

**Impact:**
- The whole project is "Postgres-native with pgvector" — pgvector is a load-bearing dependency.
- If an operator runs the migration against a Postgres instance where pgvector is not loaded (because they used the wrong image, forgot to `CREATE EXTENSION`, or the image happens to not include pgvector), the migration "succeeds" with no HNSW index. Vector search then falls back to a sequential scan over the entire `chunks` table.
- On a 100K-chunk corpus, a single vector query goes from ~10 ms (HNSW) to several seconds (seq scan). The operator sees "migration succeeded" and discovers the performance cliff later under load, with no log indicating that the index creation failed.
- The justification "test environment without pgvector" is wrong: the test environment in `docker-compose.yml` uses `pgvector/pgvector:pg16` which has the extension loaded; the CI workflow uses the same image. There is no legitimate environment in this project where pgvector is unavailable.

**Fix:** Remove the try/except. Let the migration fail loudly if pgvector isn't available:

```python
op.execute("""
    CREATE INDEX idx_chunks_embedding ON core.chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE embedding IS NOT NULL AND 'vector' = ANY(sinks)
""")
```

If a future legitimate use case requires running migrations without pgvector (e.g., a dev workflow that doesn't exercise embeddings), the right answer is a separate migration step gated on an env var, not a bare `except Exception: pass`.

---

## HIGH — should fix before Phase 6 closes

### H-1: Partition management script depends on a driver not in pyproject.toml

**Where:** `scripts/create_monthly_partition.py`, `pyproject.toml`.

```python
def sync_url(database_url: str) -> str:
    """Convert an asyncpg URL to a plain psycopg2-compatible postgresql:// URL."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

def execute_sql(database_url: str, sql: str) -> None:
    from sqlalchemy import create_engine, text
    url = sync_url(database_url)
    engine = create_engine(url, echo=False, future=True)
    ...
```

`postgresql://...` (no driver suffix) tells SQLAlchemy to use `psycopg2` by default. But `pyproject.toml` only declares `asyncpg`. Running `uv run python scripts/create_monthly_partition.py` produces:

```
sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgresql.psycopg2
```

(Or `ModuleNotFoundError: No module named 'psycopg2'`, depending on SQLAlchemy version.) The `--dry-run` path works because it doesn't open a DB connection. The real path fails.

**Fix (two options, in increasing order of cost):**

1. **Use asyncpg via SQLAlchemy's async engine** — add 5 lines of async glue to the script:
   ```python
   import asyncio
   from sqlalchemy.ext.asyncio import create_async_engine

   async def _execute(url: str, sql: str) -> None:
       engine = create_async_engine(url, echo=False)
       async with engine.connect() as conn:
           await conn.execute(text(sql))
           await conn.commit()
       await engine.dispose()

   def execute_sql(database_url: str, sql: str) -> None:
       asyncio.run(_execute(database_url, sql))
   ```
   The same DSN (`postgresql+asyncpg://...`) is used everywhere else in the project; no new dependency needed.

2. **Add `psycopg[binary]` to pyproject.toml under a `[tool.uv.dev-dependencies]` group** — but it's wasteful to pull in a second DB driver just for one script.

Recommendation: option 1.

---

### H-2: CI service surface vs test selector

**Where:** `.github/workflows/ci.yml` line 123.

```yaml
run: uv run pytest tests/ -q -m "not integration"
```

This deselects 30 tests marked `@pytest.mark.integration` — but the CI workflow still provisions Postgres, Redis, and MinIO. Two things happen:

1. The 30 deselected tests (in `tests/integration/test_session_isolation.py` and `tests/integration/test_stream_blob.py`) are the ones that exercise the DB session pool and MinIO blob streaming directly. They never run in CI. They are also the most likely candidates to catch breakage in those subsystems.
2. The rest of `tests/integration/` (`test_e2e_pipeline.py`, `test_e2e_search.py`, `test_handlers.py`, `test_auth_provisioning.py`, `test_metrics_endpoint.py`) is unmarked and DOES run in CI. These tests need the same services. So the services are needed; the question is whether the marker is doing what was intended.

The mismatch suggests Sonnet added the `-m "not integration"` filter without understanding that the marker is selectively applied only to a subset of `tests/integration/`. Either:

- **Drop the filter entirely** (`run: uv run pytest tests/ -q`) — the services are running, all integration tests will pass with infra. This is what `make test` should look like.
- **Or remove the integration marker entirely** and rely on directory convention — but that breaks a documented project convention.

Recommendation: drop the `-m "not integration"` filter. The 30 marked tests will pass against the CI's pgvector service and exercise valuable code paths (pool reset semantics, S3 streaming round-trip) that no other test covers.

---

### H-3: DELETE endpoint documentation overstates FK cascade reach

**Where:** `src/omnivore/api/routes/documents.py` line 332–333 (DELETE endpoint comment).

```python
# Delete document row — FK CASCADE handles chunks, entities, extracted_tables,
# extracted_rows, jobs, and outbox entries automatically
```

The claim about `outbox entries` is wrong. `core.outbox.aggregate_id` is declared as `UUID NOT NULL` with no foreign key constraint to `core.documents.id` — by design, because outbox entries must survive aggregate deletion in event-sourcing patterns. Verified in `src/omnivore/db/models.py` (the `Outbox` class has no `ForeignKey` on `aggregate_id`).

**Impact:** When a user deletes a document via `DELETE /v1/documents/{id}`:
- Chunks, entities, extracted_tables, extracted_rows, jobs are correctly cascade-deleted.
- **Outbox rows referencing the deleted document survive.** If those outbox rows have `published_at IS NULL` (the deletion happened before the outbox relay drained them), the next `outbox_relay` run (every 30 s) will enqueue an `ingest_dispatch` task for a now-deleted document. The worker logs `ingest.dispatch.doc_not_found` and the document_id of a non-existent row, then returns `{"status": "error", "reason": "document_not_found"}`. The outbox row is marked as published (since the enqueue succeeded), so this happens at most once per orphan.

**Severity is LOW in practice** — the worker handles missing documents gracefully and produces log noise but no data corruption. But the comment is misleading and the issue is fixable cheaply.

**Fix:**

Option A — explicit cleanup in the DELETE handler:
```python
# Before db.delete(doc):
await db.execute(
    delete(Outbox).where(
        Outbox.aggregate_id == document_id,
        Outbox.published_at.is_(None),
    )
)
```
This deletes only unpublished outbox entries, preserving the audit trail for published ones.

Option B — leave the behaviour as-is and fix only the misleading comment:
```python
# Delete document row — FK CASCADE handles chunks, entities, extracted_tables,
# extracted_rows, and jobs. Outbox entries are intentionally NOT cascaded:
# if an unpublished outbox row references this document, the worker will log
# 'document_not_found' on the next relay and continue. This is benign.
```

Recommendation: Option A. The cleanup is two SQL statements and prevents avoidable log noise. The outbox is meant to be a "drain to Redis" buffer, not a permanent audit log of aggregate operations — published rows are kept for forensics, unpublished rows referencing deleted aggregates are operationally dead.

---

## MEDIUM

### M-1: `_cap_tenant_id` set is not thread-safe and never bounds memory

**Where:** `src/omnivore/observability.py` lines 82–93.

```python
_TENANT_LABEL_CAP: int = 200
_seen_tenant_ids: set[str] = set()

def _cap_tenant_id(tenant_id: str) -> str:
    if tenant_id in _seen_tenant_ids:
        return tenant_id
    if len(_seen_tenant_ids) < _TENANT_LABEL_CAP:
        _seen_tenant_ids.add(tenant_id)
        return tenant_id
    return "__other__"
```

Two minor issues:

1. **Not thread-safe.** Reads of `len(_seen_tenant_ids)` followed by `_seen_tenant_ids.add(...)` is a TOCTOU on the cap boundary. In an asyncio-single-thread context (FastAPI app, ARQ worker) this is fine — but if a thread pool ever calls this (e.g., a sync wrapper around a handler), two threads could both pass the `< _TENANT_LABEL_CAP` check and push the set to `_TENANT_LABEL_CAP + 1` entries. Not a correctness disaster, but worth a `threading.Lock` around the `if/add` block or a comment documenting the asyncio-only assumption.

2. **Set never shrinks.** Once 200 distinct tenant IDs are seen, the 201st is normalized to `"__other__"` and all future calls for the 200 cached IDs hit the fast path. But the set retains its 200 entries forever, even if some of those tenants are deleted. Memory cost is ~12 KB at 200 entries — negligible. The principle bothers me more than the byte count.

**Fix:** Add a `threading.Lock` and a comment. Don't bother with set eviction at this scale.

---

### M-2: `ADMIN_BOOTSTRAP_TOKEN` default value mismatch between README and `.env.example`

**Where:** `README.md` line 226 vs `.env.example` line 36.

- README configuration table: `ADMIN_BOOTSTRAP_TOKEN | change-me-admin-token`
- `.env.example`: `ADMIN_BOOTSTRAP_TOKEN=change-me-before-first-run`
- `src/omnivore/config.py`: `ADMIN_BOOTSTRAP_TOKEN: SecretStr = SecretStr("change-me-admin-token")`

The README table reflects the *code default* (`config.py`), the `.env.example` reflects a *placeholder for the user to replace*. Both are defensible in isolation but reading the docs in sequence is confusing.

**Fix:** Make `.env.example` use the same default string as `config.py` and add a comment explaining it must be replaced. Or — better — change the default in `config.py` to a guaranteed-invalid sentinel like `""` and add a startup validation that refuses to boot in non-development if the admin token is empty.

---

### M-3: Grafana and the Next.js frontend share port 3000 with no resolution

**Where:** `README.md` lines 149 ("Grafana: http://localhost:3000"), 162 ("Next.js frontend | 3000"), 167 ("Grafana | 3000 (monitoring profile)"); `docker-compose.yml` line 181 (Grafana port mapping).

If a user runs both the monitoring profile AND `npm run dev` on the same host, Grafana and the frontend both want port 3000. The first to bind wins; the other fails. The README does not flag this.

**Fix (two options):**

1. **Remap Grafana to 3001 in docker-compose.yml** and update the README accordingly. This is a one-line change and is the cleanest answer for an operator running everything on one host.
2. **Add a callout to the README** explaining the conflict and how to either remap or run on separate hosts.

Recommendation: option 1.

---

### M-4: `OTEL_ENABLED=true` default produces noise on every fresh install

**Where:** `.env.example` line 66, `src/omnivore/config.py` line 45.

```
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

A user who runs `docker compose up -d postgres redis minio` (no `--profile monitoring`) and starts the API has `OTEL_ENABLED=true` but nothing listening on `localhost:4318`. `BatchSpanProcessor` silently retries; the operational logs accumulate connection errors at the OTel SDK level. Tempo unreachable warnings flood the worker startup logs in the same scenario.

The 2026-05 session's worker log shows what this looks like in practice — every span export attempt produces a warning-level entry that the operator has to learn to ignore.

**Fix:** Change the default to `OTEL_ENABLED=false`. Update the README's monitoring section to mention "remember to set `OTEL_ENABLED=true` in `.env` if you want traces". Anyone running with `--profile monitoring` is already touching the env file; this is a one-line change for them.

---

### M-5: Cost dashboard `tenant_id` legend renders UUIDs verbatim

**Where:** `observability/grafana/dashboards/cost.json` panel `legendFormat: "{{tenant_id}} / {{mime_type}}"`.

Tenant IDs are 36-character UUIDs. The dashboard's first panel — "Storage Ingested (MB/hr)" — will produce legend entries like `00000000-0000-0000-0000-000000000001 / application/pdf`, which is unreadable when more than two or three tenants are active.

**Fix:** Use a Prometheus relabel/recording rule to map `tenant_id` to a short label (first 8 chars), or change the legend to `{{tenant_id | last 8 chars}}` if Grafana supports it. Or — accept that this is a low-volume operator-only dashboard and the UUIDs are correct, just truncate visually.

Recommendation: change the legend in JSON to use Grafana's template trim feature (or accept the readability tradeoff for v1).

---

### M-6: README "Roadmap" item Phase 6 wording is imprecise

**Where:** `README.md` line 276.

```
- [x] **Phase 6 — Hardening**: Chaos tests; DB partitioning groundwork; cost dashboards; GUC pool-leak fix
```

"DB partitioning groundwork" is accurate — what shipped is migrations that convert two tables to partitioned form, plus a script for monthly maintenance. But the README doesn't communicate that:
- The partitioning migrations are destructive (drop + recreate + copy)
- They are not safe to apply to a populated production database without a maintenance window
- The HNSW index recreation may not actually work depending on the pgvector state (see C-3)

**Fix:** Add a callout in the README — or, better, in `docs/architecture.md` — noting that migrations 0011 and 0012 require a maintenance window on populated databases and have not been validated against real production data volumes.

---

## LOW

### L-1: Migration 0011 column type narrowness vs SQLAlchemy model

**Where:** `alembic/versions/0011_partition_chunks.py` line 92 (`confidence REAL`) vs `db/models.py` (`confidence: Mapped[float | None] = mapped_column(Float)`).

`SQLAlchemy.Float` maps to `REAL` (single-precision, 4 bytes) by default; `DOUBLE PRECISION` would require `Float(precision=53)` or explicit `Numeric`. So the migration is consistent with the model — but the migration comment in `0011` says `FLOAT4 (same type)`, which is a confusing way to say `REAL`. Cosmetic; the type system is correct.

**Fix:** Either accept the inconsistency in comment wording, or change the model to `Double` if higher precision is actually desired for confidence scores.

---

### L-2: `idx_jobs_status` index ordering claimed but not represented in model

**Where:** `alembic/versions/0012_partition_jobs.py` line 124 vs `db/models.py` `Job.__table_args__`.

- Migration: `CREATE INDEX idx_jobs_status ON core.jobs (status, created_at DESC)`
- Model: `Index("idx_jobs_status", "status", "created_at")` (no DESC)

SQLAlchemy `Index()` without `desc()` doesn't represent ordering. The two are functionally equivalent for indexed lookups but diverge if anyone autogenerates a migration from the model.

**Fix:** Use `Index("idx_jobs_status", "status", text("created_at DESC"))` to make the model match the migration, or drop the DESC from the migration. Either is fine.

---

### L-3: XHR upload retry on 401 re-streams the file body

**Where:** `frontend/src/lib/api.ts` lines 218–233.

```javascript
xhr.onload = async () => {
  if (xhr.status === 401) {
    const refreshed = await tryRefreshJWT();
    if (refreshed) {
      api.uploadDocument(file, onProgress)  // ← restarts the entire XHR
        .then(resolve).catch(reject);
      return;
    }
    ...
  }
};
```

On a large multi-MB upload, a 401 + refresh restarts the whole upload from scratch, re-streaming the entire file body. For a 2 GB upload (the default cap), this is potentially minutes of wasted bandwidth on the retry.

**Fix:** Acceptable for a v1 frontend. Add a one-line comment acknowledging the cost, and consider eagerly refreshing the JWT before the upload starts if `getStoredJWT()` is null or near expiry. Phase 7 or post-v1.

---

### L-4: `_seen_tenant_ids` reset semantics not exposed

**Where:** `src/omnivore/observability.py`.

If an operator wants to reset the `_seen_tenant_ids` cache (e.g., after tenant churn) without restarting the API process, there's no API to do so. This is a niche operational gap; just document it.

**Fix:** Add a comment to `_cap_tenant_id` noting the cache is process-local and never resets. If reset becomes a need, expose a debug-only `/v1/admin/observability/reset-tenant-cache` endpoint.

---

### L-5: README "What's next" section is vague

**Where:** `README.md` line 278.

```
**What's next**: PPTX/email/archive format handlers; RAPTOR/GraphRAG retrieval for multi-hop queries; billing and quota tiers; Docling PDF backend (gated on eval win > 10%).
```

Listed items are correct, but the "gated on eval win > 10%" framing is opaque to a first-time reader. Either link to `docs/architecture.md §9` where the trigger is defined, or expand inline.

**Fix:** Link to `docs/architecture.md`.

---

## Verified — no issue

- `_otel_context_processor` correctly switched from `is_recording()` to `ctx.is_valid` (M-4). ✅
- `_cap_tenant_id` is applied at every `DOCUMENTS_TOTAL` and `CHUNKS_TOTAL` call site in `worker/tasks.py`. Spot-checked lines 234, 337, 351 — all use the cap. ✅
- `STORAGE_BYTES_TOTAL` is emitted in the success path of `_run_ingest`, before the embedding step. Counter increments by `size` (the document's byte size). ✅
- `worker.tasks.on_shutdown` correctly calls `_otel_trace.get_tracer_provider().shutdown()` before the shutdown log line. ✅
- Tracer name `omnivore.worker` consistently applied in `worker/tasks.py`. ✅
- Promtail config comment about `COMPOSE_PROJECT_NAME` correctly explains the regex assumption. ✅
- Loki `derivedFields.matcherRegex` correctly matches both JSON and ConsoleRenderer log formats. ✅
- Search sinks filter (`'relational' = ANY(c.sinks)` in BM25, `'vector' = ANY(c.sinks)` in vector, both in hybrid CTEs) is correctly applied and matches the routing semantics. ✅
- DELETE endpoint correctly checks tenant ownership (`doc.tenant_id != auth.tenant_id`) and refuses in-progress documents (409). ✅
- Chaos tests are well-structured. `tests/chaos/conftest.py` provides clean fixtures. Tests assert on actual code paths (e.g., that `db.commit` is called before `pool.enqueue_job`, that 429 is returned exactly at `MAX_QUEUE_DEPTH`). 29 tests pass. ✅
- Frontend `auth.ts` correctly guards SSR with `typeof window === "undefined"`. ✅
- Frontend `AuthGuard` runs in `useEffect` (post-hydration), correctly avoiding SSR mismatch. ✅
- Migration 0010 (add `created_at` to jobs) is a simple `ADD COLUMN` with `DEFAULT now()` — backfills cleanly. ✅
- Migration 0012 (jobs range partition) correctly uses composite PK `(id, created_at)` per Postgres partitioning requirements, and the `Job` model is updated to match. ✅
- Migration 0011 correctly recreates the `tenant_isolation` RLS policy on the new partitioned table with the same predicates as migration 0008 (including the `OR bypass_rls` clause). ✅
- 396 tests pass with infra running; ruff clean across `src/`, `tests/`, `alembic/`. ✅
- `ALLOWED_ORIGINS` thread-through is one line in `config.py` and one line in `api/main.py` — clean and correct. ✅

---

## Recommended fix order (hand back to Sonnet)

**Goal:** close C-1 + C-2 + C-3 + H-1 + H-2 + H-3 + M-2 + M-3 + M-4. With these nine, Phase 6 closes cleanly as an open-source v1.

1. **C-1** — re-apply the three M-fixes in `api/main.py`. Patch is in this report; the worktree at `.claude/worktrees/agent-a349872350ec818f4` has the correct version. ~10 lines.
2. **C-2** — fix the multi-line PEM docs (README + .env.example) + add a startup length check in `lifespan()`. ~15 lines + docs.
3. **C-3** — remove the bare `try/except` around HNSW index creation in migration 0011. ~3 lines.
4. **H-1** — rewrite `scripts/create_monthly_partition.py::execute_sql` to use `asyncio.run` + `create_async_engine`. ~10 lines.
5. **H-2** — drop the `-m "not integration"` filter in `.github/workflows/ci.yml`. ~1 line.
6. **H-3** — delete unpublished outbox entries inside the DELETE handler. ~5 lines.
7. **M-2** — align `ADMIN_BOOTSTRAP_TOKEN` default between README, `.env.example`, and `config.py`. ~3 lines.
8. **M-3** — remap Grafana to port 3001 in docker-compose.yml. Update README. ~3 lines.
9. **M-4** — flip `OTEL_ENABLED` default to `false`. Update README. ~2 lines.

The Mediums M-1, M-5, M-6 and all Lows can be deferred to a v1.1 polish pass without blocking the open-source release.

---

## Test coverage state after fixes

After C-1 is re-applied, the existing `tests/unit/test_observability.py::test_otel_context_processor_uses_ctx_is_valid_not_is_recording` continues to pass. Three new tests are needed:

- One that asserts `QUEUE_DEPTH._metrics` contains the `queue="default"` and `queue="gpu"` label keys after lifespan startup completes — proves M-3 lands. (Can run via `ASGITransport` with a mocked ARQ pool.)
- One that asserts the lifespan `__aexit__` path calls `provider.shutdown()` exactly once — proves M-5 lands.
- One that asserts `instrument_app` is called when `OTEL_ENABLED=True` and not called when `OTEL_ENABLED=False` — proves M-6 lands.

After C-2 is fixed, add a test in `tests/unit/test_config.py` that asserts pydantic-settings rejects a one-line PEM (length < 200). Also test the end-to-end JWT exchange with a known-good keypair to confirm the auth flow.

After C-3 is fixed, the existing chunks partition test (if any) should run against an environment with pgvector loaded — the CI workflow's `pgvector/pgvector:pg16` service already provides this.

Once these are in place, Phase 6 ships clean.

---

**Sign-off:** Audit complete. Three CRITICAL issues block close. All are mechanical fixes with clearly-defined patches. No architectural rework required.

— Opus 4.7, acting as Senior Systems Architect
