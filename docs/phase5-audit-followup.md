# Phase 5 Audit — Follow-up Re-evaluation (2026-05-19)

**Reviewer:** Opus 4.7 (Senior Systems Architect role)
**Subject:** Sonnet 4.6's fixes for the 6 issues raised in [`docs/phase5-audit.md`](phase5-audit.md)
**Commits reviewed:** `288e662` → `a878e4e` (5 commits, 8 files, +177 / -8 lines)

---

## Verdict at a glance

**PASS WITH ONE BLOCKING REGRESSION + ONE DOCUMENTATION/DEPLOYMENT FIX.**

All 6 originally-flagged issues are **structurally resolved**. 352 tests pass, ruff is clean. The fixes follow the recommended patterns from the original audit. However, the H-4 fix introduced **one new HIGH-severity gap** (worker `/metrics` endpoints have no auth — the protection only covers the API process) and **one HIGH-severity deployment defect** (`prometheus.yml` uses `${METRICS_AUTH_TOKEN}` env-var substitution, which Prometheus does **not** expand in `authorization.credentials`).

Both are small, contained fixes — neither requires re-architecting any of the original work. Close them and Phase 5 is shippable.

---

## Original-audit issues — re-verification

| ID | Fix location | Verified | Notes |
|----|--------------|----------|-------|
| **C-1** Worker metrics not scraped | `tasks.py:411`, `gpu_main.py:23`, `docker-compose.yml:73-74,96-97`, `prometheus.yml:13-22` | ✅ | `start_http_server` on 9101/9102, ports published, scrape jobs added. `OSError` guard added (correctly addresses port-bind crash). Regression test in `test_metrics_endpoint.py:52-74` proves `DOCUMENTS_TOTAL` is wired to the global registry. |
| **H-1** GPU worker no tracing | `gpu_main.py:21` | ✅ | `setup_tracing(settings)` is called. |
| **H-2** Workers no JSON logs / no trace_id | `tasks.py:409`, `gpu_main.py:20` | ✅ | `configure_logging(settings)` is the first call after `get_settings()` in both. Order is correct (before `setup_tracing`) so the `observability.tracing.configured` log carries trace_id semantics from the start. |
| **H-3** Unbounded route cardinality | `api/main.py:55-60` | ✅ | Returns `"__unmatched__"` for 404s, `"__unknown__"` as defensive fallback. Regression test (`test_unmatched_route_label`) hits `/nonexistent-path-xyz` and asserts the label appears. |
| **H-4** `/metrics` tenant_id leak | `api/main.py:127-136`, `config.py:49`, `prometheus.yml:10-25` | ⚠️ **Partial** | API process is protected. **Worker `/metrics` is NOT.** See NEW-H-1 below. |
| **H-5** `tenant_id` missing from spans | `auth/dependencies.py:11,40-43` | ✅ | `span.set_attribute("tenant_id", ...)` + `principal_type` after auth resolves. Uses `_otel_trace.get_current_span()` which sits inside the FastAPIInstrumentor http.server span — correct attachment point. |

**Acceptance bar from the original audit:** "C-1, H-1, H-2, H-3, H-4 closed (H-5 recommended)." 

5 of 6 are fully closed; **H-4 is half-closed** (API protected, workers not). The original audit's intent — "tenant UUIDs are enumerable from any browser" — is still satisfiable against worker metric endpoints.

---

## NEW issues introduced by the fixes

### NEW-H-1: Worker `/metrics` endpoints (9101, 9102) have no authentication. H-4 protection is asymmetric.

**Where:** `src/omnivore/worker/tasks.py:411-415`, `src/omnivore/worker/gpu_main.py:22-26`, `docker-compose.yml:73-74,96-97`.

**Reproduction:**
```bash
$ uv run python -c "
import inspect
from prometheus_client import start_http_server
print(inspect.signature(start_http_server))
"
# (port: int, addr: str = '0.0.0.0', registry: ..., certfile: ..., keyfile: ...)
```

`prometheus_client.start_http_server` only supports mTLS (cert-based) auth via `certfile`/`keyfile`. It has **no bearer-token check**. The worker processes therefore serve `/metrics` to anyone on the network — and `docker-compose.yml` publishes ports `9101:9101` and `9102:9102` to `0.0.0.0` on the host.

**Impact:** the API-side fix correctly protects `DOCUMENTS_TOTAL{tenant_id}`, `CHUNKS_TOTAL{tenant_id}`, etc. from being read via `api:8000/metrics`. But those exact same metric series are also emitted from worker processes (where the increments actually happen) and exposed via `worker:9101/metrics` and `worker-gpu:9102/metrics` with no auth. An operator who sets `METRICS_AUTH_TOKEN` expecting full protection gets a false sense of security: the most sensitive data leaks through the unguarded worker endpoints.

Severity is **HIGH** because:
1. The exact threat model that justified H-4 (tenant UUID enumeration) is unmitigated.
2. The asymmetric behavior is surprising — an operator who reads the API fix won't expect worker endpoints to be open.
3. In Docker-compose dev environments the host port mapping makes the endpoints reachable from any process on the dev box, including browsers via CORS-less `fetch`.

**Fix options:**

1. **Don't publish worker ports to the host** (cleanest for production):
   ```yaml
   worker:
     # remove the "ports:" block — Prometheus reaches the worker via the
     # Compose internal network (worker:9101); no host mapping needed.
   ```
   But this breaks dev workflows that scrape worker metrics from a host-side Prometheus. Acceptable trade-off: documentation note that monitoring requires `docker compose --profile monitoring`.

2. **Wrap `start_http_server` with a custom WSGI auth middleware**:
   ```python
   from wsgiref.simple_server import WSGIServer, make_server
   from prometheus_client.exposition import make_wsgi_app, _SilentHandler
   
   def _auth_wrap(app, token: str | None):
       def wsgi(environ, start_response):
           if token is not None:
               header = environ.get("HTTP_AUTHORIZATION", "")
               if not secrets.compare_digest(header, f"Bearer {token}"):
                   start_response("403 Forbidden", [("Content-Type", "text/plain")])
                   return [b"Forbidden"]
           return app(environ, start_response)
       return wsgi
   
   # Replace start_http_server with manual server construction
   ```
   Roughly 20 lines of code. Symmetric with API behavior.

3. **Mount a small FastAPI app inside the worker** that exposes the same protected `/metrics` endpoint as the API. Reuses existing code but adds asyncio loop overhead.

**Recommendation:** **option 1** for production deployments (don't publish worker ports) plus **option 2** as belt-and-suspenders if the operator does choose to expose them. Either alone closes NEW-H-1.

---

### NEW-H-2: `prometheus.yml` uses `${METRICS_AUTH_TOKEN}` env-var substitution, which Prometheus does not expand in `authorization.credentials`. Production scraping will 403.

**Where:** `observability/prometheus.yml:11,18,25`.

```yaml
- job_name: omnivore-api
  ...
  authorization:
    credentials: "${METRICS_AUTH_TOKEN}"
```

**Reproduction (architectural — verified against Prometheus docs):**

Prometheus does **not** expand `${VAR}` references inside YAML config values by default. The `--enable-feature=expand-external-labels` flag (Prometheus 2.27+) enables expansion only inside `global.external_labels`. For all other config values, including `authorization.credentials`, the YAML is read verbatim.

Therefore Prometheus will send the literal string `${METRICS_AUTH_TOKEN}` as the bearer token to every scrape. When `METRICS_AUTH_TOKEN` is set on the API side, every scrape returns 403 and dashboards go blank.

**Why this isn't caught by the current test suite:** the integration test (`metrics_auth_results`) drives the FastAPI app directly via `ASGITransport` — Prometheus is never in the loop. The end-to-end "Prometheus → API" path isn't exercised in CI.

**Why this currently "works" in dev:** the default is `METRICS_AUTH_TOKEN=None`, which makes the API skip the auth check. Prometheus's literal `${METRICS_AUTH_TOKEN}` header is ignored. The first time someone sets the token in production, scrapes start failing.

**Impact:** Phase 5's promise — "any bottleneck, error, or latency spike is diagnosable in one place (Grafana)" — silently breaks the moment an operator hardens production by setting the auth token. They will see dashboards with no data and trace through the API logs (which show 403s) only to discover the YAML substitution was a no-op.

**Fix (one of):**

1. **`credentials_file:` pattern** (idiomatic for Prometheus):
   ```yaml
   authorization:
     credentials_file: /etc/prometheus/auth_token
   ```
   Mount the token as a file in `docker-compose.yml`:
   ```yaml
   prometheus:
     volumes:
       - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro
       - ./observability/auth_token:/etc/prometheus/auth_token:ro
   ```
   The file content is just the token. Operator generates it during deploy.

2. **`envsubst` at container start** (works but adds a layer):
   ```yaml
   prometheus:
     entrypoint: ["/bin/sh", "-c", "envsubst < /etc/prometheus/prometheus.yml.tpl > /tmp/prom.yml && /bin/prometheus --config.file=/tmp/prom.yml ..."]
     environment:
       METRICS_AUTH_TOKEN: ${METRICS_AUTH_TOKEN}
   ```
   And rename `prometheus.yml` → `prometheus.yml.tpl`.

3. **Document the gap clearly and shift the responsibility to the operator** — but this contradicts the "auto-provisioned" promise of Phase 5.

**Recommendation:** **option 1**. It's how every other Prometheus deployment handles this. Two-line YAML change, one-line `docker-compose` change, and an explicit comment in the file pointing operators at it.

---

## Pre-existing items (not regressed, still open from original audit)

These were noted in the original audit and were not in scope for the fix pass. They remain open and should be tracked separately for Phase 6:

- **M-1**: Metric cardinality unbounded as tenant count grows (still applies).
- **M-3**: Counters/gauges show "no data" until first observation (still applies).
- **M-4**: `_otel_context_processor` uses `is_recording()` vs `is_valid` — note that the H-5 fix in `auth/dependencies.py:40-43` reuses the same `is_recording()` pattern. Not a regression (matches the existing convention), but the same fix should be applied uniformly when M-4 is addressed.
- **M-5**: `BatchSpanProcessor` not shut down on lifespan exit.
- **M-6**: `try: FastAPIInstrumentor.instrument_app(app); except: pass` still swallows errors.
- **L-1** through **L-7**: all carry over (Promtail regex hardcoding, Loki schema date, Tempo retention, etc.).

---

## Verified — clean fixes

- `start_http_server(port=9101/9102)` is correctly guarded by `try/except OSError` with informative log on both paths. No worker crashes on port conflict.
- `configure_logging` precedes `setup_tracing` in both `on_startup` functions — early log events get trace_id correctly.
- `_route_template` correctly bounds cardinality at the FastAPI route count + 1 (`__unmatched__`).
- `secrets.compare_digest` is constant-time — the H-4 token check resists timing attacks.
- The H-5 implementation correctly uses `_otel_trace.get_current_span()` inside `require_auth`, which fires inside the FastAPIInstrumentor `http.server` span — the attribute lands on the right span.
- Test `test_unmatched_route_label` correctly exercises the 404 path.
- Test `test_metrics_no_auth_returns_403`, `test_metrics_wrong_token_returns_403`, `test_metrics_correct_token_returns_200`, `test_metrics_correct_token_body_is_prometheus` correctly cover the three auth states.
- `test_documents_total_metric_increments_on_success` proves the metric is registered with the global registry and that `.labels(...).inc()` is read back correctly — the right in-process regression guard for C-1.
- Ruff is clean on `src/`, `tests/`, `eval/`. 352 tests pass.
- Import ordering on both modified files is alphabetically correct.
- The `compose-config` validates and 5/5 monitoring containers start correctly per the existing smoke test path.

---

## MINOR observations (non-blocking)

### MIN-1: HTTP 403 vs 401 for missing auth token.

`api/main.py:135`: `return Response(status_code=403)`.

Semantically, 401 Unauthorized is the correct status for "no valid auth provided"; 403 Forbidden means "authenticated but not allowed." For consistency with HTTP semantics (and to play nicely with monitoring tools that treat 401 differently from 403), prefer 401 with a `WWW-Authenticate: Bearer` header. Non-blocking; flagged for polish.

### MIN-2: Empty `METRICS_AUTH_TOKEN` ("") would still satisfy the check.

`api/main.py:131`: `if token is not None`. If an operator misconfigures with `METRICS_AUTH_TOKEN=""`, the check runs but `expected = "Bearer "` — anyone sending `Authorization: Bearer ` would match. Edge case; the failure mode is "auth check is effectively absent," not "auth is bypassed by malicious input."

**Fix:** `if token is not None and token.get_secret_value():`

### MIN-3: `test_documents_total_metric_increments_on_success` permanently writes a label set to the global registry.

The label combination `{status: "indexed", tenant_id: "c1-regression", mime_type: "text/plain"}` persists for the lifetime of the test process. Other tests reading `DOCUMENTS_TOTAL.collect()` see this extra series. Cosmetic; doesn't affect any other test.

### MIN-4: `metrics_auth_results` fixture is function-scoped — runs the 3-request HTTP sequence 4 times (once per test).

Make it `scope="module"` to run once. Saves ~0.5s per test invocation.

### MIN-5: `_route_template` line 59 fallback `getattr(route, "path", "__unknown__")` will never fire.

All FastAPI / Starlette routes have a `path` attribute. The fallback is dead defensive code. Either keep with a comment ("defensive — never expected to fire") or remove.

---

## Recommended hand-back

**Single small follow-up commit** can close both NEW-H-1 and NEW-H-2:

1. Remove `ports:` blocks from `worker` and `worker-gpu` in `docker-compose.yml` (NEW-H-1, option 1). Prometheus reaches them via the Compose internal network; host publication isn't needed.
2. Replace `credentials: "${METRICS_AUTH_TOKEN}"` with `credentials_file: /etc/prometheus/auth_token` in all three scrape jobs (NEW-H-2, option 1). Add a one-line volume mount for the token file in the `prometheus:` service. Add a `.gitignore` for `observability/auth_token` (so a real production token doesn't get committed) and a `observability/auth_token.example` containing "change-me" so operators know to create it.
3. Optionally: address MIN-2 (`and token.get_secret_value()`) in the same commit — one extra condition.

Total surface: ~10 lines across 2 files + 1 new example file. Estimated 10 minutes of work plus a re-run of the test suite. After that commit lands, Phase 5 flips to **unconditional PASS** with the only remaining items being the original-audit MEDIUMs and LOWs deferred to Phase 6.

**Acceptance bar for unconditional Phase 5 closure:**
- NEW-H-1 closed (worker ports unpublished OR auth wrapper added)
- NEW-H-2 closed (`credentials_file:` pattern)
- 352 tests still pass
- ruff clean
- `docker compose --profile monitoring up -d` smoke test passes
