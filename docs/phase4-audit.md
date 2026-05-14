# Phase 4 Audit — 2026-05-14

**Reviewer:** Opus 4.7 (Senior Systems Architect role)
**Subject:** Sonnet 4.6's Phase 4 implementation (multi-tenant auth + RLS + routing enforcement)
**Verdict:** **PASS WITH BLOCKING FIXES** — 2 CRITICAL bugs + 5 HIGH issues. The structural work (migrations, scope model, M-1/M-2/M-3 wiring, JWT/key crypto) is sound; the production-facing seams have holes that the 327-test green suite does not catch.

---

## Scope built

- `src/omnivore/auth/` — full module: `context.py` (frozen `AuthContext`), `api_key.py` (Argon2id, prefix indexing, sha256 cache key), `jwt.py` (RS256 issue/decode), `cache.py` (Redis 30s auth cache), `dependencies.py` (`require_auth`, `require_scope`, `get_db_for_tenant`), `rate_limit.py` (Lua token bucket), `errors.py`, `admin_token.py`.
- `src/omnivore/api/routes/admin.py`, `auth_route.py`, `tenant.py` — admin provisioning, JWT exchange, tenant self-service.
- 5 migrations (0005–0009): tenant columns, `core.api_keys` table, `extracted_rows.tenant_id`, FORCE ROW LEVEL SECURITY on 6 tables, `chunks.sinks[]` + `matched_rule_id` for routing audit.
- Phase 3 M-items closed: `evaluate_policy()` returns `(sinks, matched_rule_id)`; `_run_ingest` fetches `tenant.config["routing_policy"]`; vector embedding is skipped when `"vector" not in sinks`.
- 44 new tests (34 unit + 10 integration); 327 total passing; ruff clean.

---

## CRITICAL — block merge

### C-1: Admin endpoints don't commit. Tenants and API keys returned by `/v1/admin/*` are silently rolled back.

**Where:** `src/omnivore/api/routes/admin.py` (`create_tenant`, `create_api_key`, `update_tenant`, `revoke_api_key`) and `src/omnivore/api/routes/tenant.py::create_own_key`.

**Reproduction:**
```python
async with admin_session() as db:
    db.add(tenant); await db.flush()
    db.add(api_key); await db.flush()
    # ← no commit
return APIResponse(... data={"tenant_id": ..., "api_key": raw_key, ...})
```

`admin_session()` in `src/omnivore/db/session.py:48-58` exits the underlying `async with AsyncSessionLocal()` without calling `commit()`. SQLAlchemy 2.0 async sessions roll back on close. Empirically confirmed:

```
Tenant created in session: 8ceab4e9-720c-4ce3-a484-be66b8166e01
Tenant after session close: None
```

**Impact:** every admin write returns 201 with a phantom ID. Operators trying the documented bootstrap flow (`POST /v1/admin/tenants` → use the returned `api_key`) will see every subsequent request return 401 because no row exists. Tenant suspension, key revocation, and config patches all silently no-op.

**Why no test caught this:** an earlier draft of the integration test had `test_admin_create_tenant_returns_api_key` that would have failed at the follow-up GET. It was removed during the test rewrite. The remaining `test_admin_create_tenant_requires_token` only checks 401 without token — it never exercises the happy path.

**Fix (minimal):** add `await db.commit()` at the end of each admin handler **OR** make `admin_session()` commit on clean exit:

```python
@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("SET app.bypass_rls = 'on'"))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
```

Add a positive integration test: `POST /v1/admin/tenants` with valid token → 201 → GET `/v1/documents` with the returned key → 200.

---

### C-2: `invalidate_tenant_auth` is dead code. The cache prefix is wrong; revocations have a 30-second blast-radius window.

**Where:** `src/omnivore/auth/cache.py:29-35`.

```python
async def invalidate_tenant_auth(tenant_id: uuid.UUID) -> None:
    async with _client() as r:
        pattern = f"auth:tenant:{tenant_id}:*"   # ← prefix never written
        keys = [k async for k in r.scan_iter(pattern)]
        ...
```

The actual cache keys written are `auth:apikey:<sha256(raw_key)>` (`api_key.py:49`) and `auth:jwt:<sha256(token)>` (`dependencies.py:127`). The `auth:tenant:` prefix is never produced. `scan_iter` returns nothing. `delete` is never called.

**Impact:** the only caller (`tenant.py::update_tenant_config` line 74-75) believes it's invalidating the cache. It isn't. Combined with C-1, tenant suspension is double-broken: the admin PATCH doesn't persist the suspended status, AND even if it did, the auth cache would still serve the old scopes/active status for 30 seconds. Same goes for individual key revocation.

**Fix:** maintain a secondary index keyed by tenant. Two clean options:

1. **Dual-key write:** on cache SET, also write a sentinel `auth:tenant:<tid>:apikey:<digest>` with a TTL matching the main entry. `invalidate_tenant_auth` scans the secondary, then deletes both keys per match.
2. **Version counter:** store `auth:tenant:<tid>:v` (incremented on tenant changes). Every cached entry carries `tenant_version`. `_resolve_*` compares cached version to current; mismatch → re-resolve.

Option 2 is cheaper. Either works.

---

## HIGH — should fix before Phase 5

### H-1: `get_db_for_tenant` doesn't set the RLS GUC. Migration 0008 is mostly cosmetic for API routes.

**Where:** `src/omnivore/auth/dependencies.py:48-59`.

Sonnet correctly diagnosed the `SET LOCAL + session.begin()` transaction conflict and removed the `session.begin()` wrapper. But the fix also removed the `SET` entirely from API route sessions. Workers correctly use `tenant_session()` (which does `SET app.current_tenant_id = ...`); API routes do not.

Today this is masked because `omnivore` is a Postgres superuser and FORCE RLS doesn't apply to superusers. In a non-superuser production deployment, list endpoints would return zero rows (the GUC defaults to NULL, the policy fails closed) and PK-lookup endpoints would 404 every request — UNLESS a previous request on the same pooled connection left a stale `SET` from another tenant, in which case it would serve **wrong tenant** data.

**Fix:** use session-level `SET` mirroring `tenant_session`, and reset on the way out so connection reuse cannot leak:

```python
async def get_db_for_tenant(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f"SET app.current_tenant_id = '{auth.tenant_id}'"))
            yield session
        finally:
            try:
                await session.execute(text("RESET app.current_tenant_id"))
            except Exception:
                pass
            await session.close()
```

Explicit `doc.tenant_id != auth.tenant_id` checks remain as the primary guard (correct defense-in-depth), but RLS must be wired for production.

---

### H-2: Rate limiting is only applied to `POST /v1/documents`. Every read endpoint is unbounded.

**Where:** `src/omnivore/api/routes/documents.py:42` — only `upload_document` calls `check_rate_limit`. `get_document`, `get_document_entities`, `list_documents`, `retry_document`, `search` (`api/routes/search.py`), `list_handlers` — none of them invoke the limiter.

Architect plan specified `Depends(rate_limit(cost=1))` on every authed route. An attacker with a valid key can flood `/v1/search` (which triggers embedding lookups + Postgres queries) at unlimited QPS, contradicting the tenant-fairness promise.

**Fix:** factor into a dependency and apply uniformly:

```python
def rate_limited(cost: int = 1):
    async def _dep(
        auth: Annotated[AuthContext, Depends(require_auth)],
        response: Response,
    ) -> None:
        rl = await check_rate_limit(auth.tenant_id, cost=cost)
        for k, v in _rl_headers(rl).items():
            response.headers[k] = v
        if not rl.allowed:
            raise RateLimitedError()
    return _dep
```

Then add `Depends(rate_limited())` (default cost=1) to every auth-protected route, `Depends(rate_limited(cost=10))` to upload and retry.

---

### H-3: Connection storm in `cache.py` and `rate_limit.py` — new Redis client per request.

**Where:** `src/omnivore/auth/cache.py:14-15` (`_client()` returns a fresh `aioredis.from_url(...)` every call) and `src/omnivore/auth/rate_limit.py:65` (`aioredis.from_url(...)` per `check_rate_limit`).

Each `from_url` instantiates a new `Redis` client with its own connection pool. Under load: rate-limit + auth-cache GET + auth-cache SET = 3 connection-pool churns per request. The ARQ pool already lives on `app.state.arq_pool`. Auth/rate-limit should reuse it or maintain a parallel singleton.

**Fix:** module-level Redis client wired up in the FastAPI lifespan (mirror `app.state.arq_pool`). For workers, mirror in `on_startup`. Replace `_client()` and the in-function `aioredis.from_url` with a getter that returns the singleton.

---

### H-4: `verify_key` only catches `VerifyMismatchError`. A malformed `key_hash` row 500s the request.

**Where:** `src/omnivore/auth/api_key.py:32-36`.

```python
def verify_key(stored_hash: str, raw_key: str) -> bool:
    try:
        return _ph.verify(stored_hash, raw_key)
    except VerifyMismatchError:
        return False
```

`argon2.PasswordHasher.verify` raises `InvalidHashError` (sibling of `VerifyMismatchError`, not a subclass) when the stored hash isn't a valid Argon2 PHC string. Any future schema change, manual edit, or migration glitch that corrupts `key_hash` returns 500 instead of 401.

**Fix:**
```python
from argon2.exceptions import InvalidHashError, VerificationError
...
    except VerificationError:   # parent of VerifyMismatchError, InvalidHashError, VerifyMismatchError
        return False
```

---

### H-5: Admin `PATCH /v1/admin/tenants/{id}` skips routing-policy validation.

**Where:** `src/omnivore/api/routes/admin.py:116-133`.

```python
async def update_tenant(tenant_id: uuid.UUID, body: dict, ...):
    ...
    for field in ("display_name", "status", "config"):
        if field in body:
            setattr(tenant, field, body[field])
```

The tenant self-service path (`tenant.py::update_tenant_config:62-65`) validates `routing_policy` with `validate_policy()` and 422s on invalid input. Admin path doesn't. An admin can persist `{"routing_policy": {"rules": "not-a-list"}}`, which crashes `_run_ingest` 30 seconds later inside `evaluate_policy` with no clear origin trail.

**Fix:** mirror the self-service validation, or replace `body: dict` with a Pydantic `UpdateTenantRequest` that runs `validate_policy()` in a field validator.

---

## MEDIUM

### M-1: M-1/M-2/M-3 closure is structurally wired but not E2E-tested.

`test_routing.py` exercises `evaluate_policy()` as a pure function. `test_routing_decision_populated` only asserts the document-level summary exists. **Nothing** verifies the actual gating end-to-end: configure a tenant `routing_policy` with a `confidence_lt` rule, upload a document with mixed-confidence chunks, assert that `chunks[i].embedding IS NULL` and `chunks[i].sinks == []` for the dropped chunks, and that they don't appear in `/v1/search?mode=vector`.

**Resolution:** add an integration test that does the full loop. Without it, M-1 can regress silently.

### M-2: No positive admin test (would have caught C-1).

Add a single integration test: provision a tenant via the admin endpoint with a valid `ADMIN_BOOTSTRAP_TOKEN`, then GET `/v1/documents` with the returned `api_key` → 200. This is the simplest test that catches C-1 and prevents regressions.

### M-3: `BM25 search` doesn't filter by `sinks`. M-1 enforcement is one-sided.

Currently:
- Vector search: chunks with `'vector' NOT IN sinks` have `embedding IS NULL` → naturally excluded by `WHERE c.embedding IS NOT NULL` and the partial HNSW index.
- BM25 search: filters only by `tenant_id` and `content_tsv @@ q`. A chunk with `sinks=[]` (explicitly dropped by policy) is still BM25-searchable because the `tsvector` is `GENERATED ALWAYS AS (...) STORED`.

For the default policy this doesn't matter (no rule produces `sinks=[]`). For a tenant policy with `transcript + confidence_lt: 0.6 → sinks=[]`, the dropped chunks still appear in BM25 results. Either filter the BM25 query (`AND cardinality(sinks) > 0`), or skip writing the chunk row entirely when `sinks=[]`.

### M-4: `create_tenant.test` is unreachable code.

`admin.py:50`: `generate_api_key(test=body.test if hasattr(body, "test") else False)`. `CreateTenantRequest` has no `test` field; `hasattr` is always False. Test-namespaced keys (`omn_test_`) cannot be minted via the admin endpoint. Either add `test: bool = False` to `CreateTenantRequest` or delete the dead branch.

### M-5: Lua script registered on every `check_rate_limit` call.

`rate_limit.py:66`: `r.register_script(_LUA_SCRIPT)` runs every call — recomputes the SHA1 of the script, builds a `Script` instance. Negligible per-call cost but unnecessary churn. Hoist to module level (gated on H-3's Redis singleton refactor so the script is bound to a stable client).

---

## LOW

### L-1: JWT decode does not enforce `iss` or `aud` claims.

`jwt.py:43-48` requires `["exp", "iat", "sub", "tenant_id", "scopes"]` but doesn't verify `iss="omnivore"` or any `aud`. Add `options={"require": [..., "iss"]}` and pass `issuer="omnivore"`. Add an `aud` claim (e.g., `"omnivore-api"`) on issue and verify it on decode for hardening against token reuse across services.

### L-2: `_resolve_api_key` expires_at timezone handling.

`dependencies.py:91`: `matched.expires_at.replace(tzinfo=UTC)`. asyncpg returns `TIMESTAMPTZ` as already-tz-aware datetimes. `.replace(tzinfo=UTC)` rebinds the tzinfo without converting — for naïve datetimes that's fine, for aware datetimes it silently changes the wall-clock value. Use `matched.expires_at.astimezone(UTC)`, or just `datetime.now(UTC) > matched.expires_at` since both are tz-aware.

### L-3: `RateLimitResult.reset_after_seconds` off-by-one.

`rate_limit.py:47`: `int((cost - remaining) / refill_rate) + 1` always overestimates by ≤1s. Harmless (errs on the safe side); flagged for completeness.

---

## Verified — no issue

- Argon2id parameters (`memory_cost=65536` KiB = 64 MiB, `time_cost=2`, `parallelism=1`) match OWASP minimum. ✅
- RS256 over HS256 is the correct call — worker doesn't hold the signing key. ✅
- `decode_token` uses `algorithms=[settings.JWT_ALGORITHM]` (not the unverified-header value) — closes the "alg: none" attack. ✅
- Explicit `doc.tenant_id != auth.tenant_id` checks on every PK lookup (`get_document`, `get_document_entities`, `retry_document`). Correct defense-in-depth. ✅
- Partial HNSW index `WHERE embedding IS NOT NULL AND 'vector' = ANY(sinks)` (migration 0009) — elegant; vector search naturally excludes mis-embedded rows. ✅
- M-2 wiring: `_run_ingest:139-140` reads `tenant.config["routing_policy"]` and passes it through. ✅
- M-3 wiring: `evaluate_policy` returns `(sinks, matched_rule_id)`; `chunk_rows[i].matched_rule_id` is persisted. ✅
- `outbox_relay` correctly uses `admin_session()` and calls `await db.commit()` at line 327. ✅
- Lua token-bucket logic is sound (atomic refill+consume, no race; `EXPIRE 3600` reclaims abandoned buckets). ✅
- Constant-time admin token comparison via `hmac.compare_digest` (`admin_token.py:9`). ✅
- All 5 migrations apply and downgrade cleanly. ✅
- Test infrastructure (module-scoped `asyncio.run()` mirroring `test_e2e_pipeline.py`) is the correct pattern for the asyncpg pool topology. ✅
- The `engine.dispose()` workaround in `pipeline_results._run()` is a clean fix for cross-`asyncio.run()` pool staleness. ✅
- AuthError → APIResponse error mapping via `auth_exception_handler` is wired in `api/main.py`. ✅

---

## Recommended next session plan (hand back to Sonnet)

**Goal:** close C-1 + C-2 and add the load-bearing positive test.

1. **Fix C-1**: change `admin_session()` to commit on clean exit (single change, covers admin.py + tenant.py:create_own_key). Verify by re-running the empirical Python snippet in this audit and checking that the tenant row persists.
2. **Fix C-2**: replace `invalidate_tenant_auth` with the version-counter approach. Bump `auth:tenant:<tid>:v` on every API key revoke, tenant suspend, and tenant config update. Update `_resolve_api_key` and `_resolve_jwt` to read and compare versions.
3. **Add M-2 test**: positive admin-create-tenant flow inside the existing `test_auth_provisioning.py::auth_results` fixture. Use a real `ADMIN_BOOTSTRAP_TOKEN` set via `patch.dict(os.environ, ...)` and `get_settings.cache_clear()`. Assert that the returned `api_key` works for a subsequent GET.
4. **Fix H-1**: add `SET app.current_tenant_id = '{auth.tenant_id}'` + `RESET` to `get_db_for_tenant`. Tests should still pass (no behavior change for superuser).
5. **Fix H-2**: extract `rate_limited(cost=1)` dependency and apply to all authed routes. `cost=10` on `upload_document` and `retry_document`.
6. **Fix H-3**: add a module-level Redis singleton in `auth/cache.py` and `auth/rate_limit.py`. Initialize/dispose in `api/main.py` lifespan and `worker/tasks.py:on_startup`/`on_shutdown`.
7. **Fix H-4**: broaden `verify_key`'s except clause to `VerificationError`.
8. **Fix H-5**: replace `body: dict` in `admin.update_tenant` with a Pydantic model that validates `routing_policy`.
9. **Defer to Phase 5 or document:** M-1 (E2E routing test), M-3 (BM25 sinks filter), M-4 (dead code), M-5 (Lua hoist), L-1/L-2/L-3.
10. **Preflight after fixes:**
    ```bash
    docker compose up -d postgres redis minio
    uv run alembic upgrade head
    uv run pytest tests/ -q                          # expect 328+ passed (positive admin test added)
    uv run ruff check src/ tests/ eval/             # clean
    uv run python -m eval.harness                   # 8/9 (txt-002 pre-existing)
    ```

**Acceptance bar for Phase 4 closure:** C-1, C-2 closed + M-2 test added. With those three, this audit flips to **PASS**.
