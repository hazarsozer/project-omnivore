# Phase 5 Audit — 2026-05-19

**Reviewer:** Opus 4.7 (Senior Systems Architect role)
**Subject:** Sonnet 4.6's Phase 5 implementation (OTel traces + Prometheus metrics + Loki/Promtail logs + Grafana dashboards)
**Verdict:** **PASS WITH BLOCKING FIXES** — 1 CRITICAL + 5 HIGH issues. The structural work is correct, the spec sections are present, the 346-test green suite confirms the API process is wired sensibly — but **the production-facing behavior of the worker-side half of this phase is largely non-functional and the public `/metrics` surface leaks tenant identity**. Phase 5 cannot ship as-is.

---

## Scope built

- **`src/omnivore/observability.py`** — 8 prometheus_client metric singletons, `setup_tracing(settings)`, `get_tracer(name)`.
- **`src/omnivore/logging_config.py`** — `_otel_context_processor` (injects `trace_id` / `span_id`) wired into the shared processor list. `configure_logging(settings)` chooses ConsoleRenderer for `development`, JSONRenderer otherwise.
- **`src/omnivore/config.py`** — 4 new settings: `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `METRICS_ENABLED`.
- **`src/omnivore/api/main.py`** — lifespan calls `configure_logging` then `setup_tracing`; `_PrometheusMiddleware` records `omnivore_http_requests_total` + `omnivore_http_duration_seconds`; `_poll_queue_depth` background task; `FastAPIInstrumentor.instrument_app` (wrapped in try/except); explicit `@app.get("/metrics")` (mount approach was rejected because Starlette mount issues a trailing-slash 307).
- **`src/omnivore/worker/tasks.py`** — `ingest_dispatch`, `gpu_ingest_dispatch`, `_run_ingest` wrapped with spans; per-chunk and per-document counters; `INGEST_DURATION` histogram observed on both success and failure; W3C `traceparent` extracted from `_otel_traceparent` kwarg and used as parent context.
- **`src/omnivore/api/routes/documents.py`** — `TraceContextTextMapPropagator().inject(carrier)` on both `enqueue_job` call sites (`upload_document`, `retry_document`).
- **`src/omnivore/pipeline/embeddings.py`** — `embed_chunks` wrapped with `omnivore.embeddings.batch` span and `EMBEDDINGS_TOTAL` counter; `_embed_chunks_impl` carries the original body.
- **`src/omnivore/api/routes/search.py`** — `SEARCH_DURATION.labels(mode=...).observe(...)` after each query resolves.
- **`src/omnivore/db/session.py`** — `reset_guc()` helper (rollback → RESET → commit) hardening NEW-H-1; `admin_session` and `tenant_session` use it on exit.
- **`src/omnivore/auth/dependencies.py`** — `SET LOCAL app.bypass_rls = 'on'` inside the three resolver helpers, fixing the GUC pool-checkout leak.
- **Monitoring stack** — 5 services behind `--profile monitoring`: tempo, prometheus, loki, promtail, grafana. 4 new volumes. Grafana anonymous Admin.
- **Tempo v3** — `compactor:` stanza removed (Tempo v3 chokes on it). Block retention defaults to 336h instead of spec'd 48h.
- **Grafana** — 3 dashboards auto-provisioned (Pipeline Overview, Search, Tenants); 3 datasources (Prometheus default, Loki with derivedField regex → Tempo, Tempo with lokiSearch).
- **Tests** — 11 new unit + integration tests (9 in `test_observability.py`, 4 in `test_metrics_endpoint.py`, 4 in `test_session_isolation.py`). 346 passing total; ruff clean.

The work that exists is **correctly structured**. The audit below is about what is **missing or broken at runtime**, not about what was built poorly.

---

## CRITICAL — block Phase 5 closure

### C-1: Worker-side metrics are emitted into a process-local registry that no scraper reads. The four most important pipeline metrics show **no data** in production.

**Where:** `src/omnivore/worker/main.py:11-25` (`WorkerSettings`), `src/omnivore/worker/gpu_main.py:27-37` (`GpuWorkerSettings`), `observability/prometheus.yml:5-9` (only `api:8000` is scraped).

**Reproduction (architectural — no docker run needed):**
- `prometheus-client`'s default `REGISTRY` is process-local. Counter increments inside one Python process are invisible to any other process unless that process exposes an HTTP server.
- `WorkerSettings.on_startup = on_startup` (`worker/tasks.py:405`) calls `setup_tracing(settings)` but starts **no HTTP server**.
- `arq.run_worker(WorkerSettings)` is a CLI-style runner with no HTTP exposition built in.
- `prometheus.yml` declares exactly one job: `targets: ["api:8000"]`.
- Therefore: every `DOCUMENTS_TOTAL.labels(...).inc()`, `CHUNKS_TOTAL.labels(...).inc()`, `INGEST_DURATION.labels(...).observe(...)`, and `EMBEDDINGS_TOTAL.labels(...).inc()` call inside `_run_ingest` and `embed_chunks` writes into a registry that is never queried.

Grep confirms there is zero worker-side metrics machinery:

```bash
$ grep -rn "start_http_server\|make_asgi_app\|/metrics\|PROMETHEUS_MULTIPROC" src/ observability/ docker-compose.yml
src/omnivore/api/main.py:125-128  # the only HTTP exposition — in the API process
observability/prometheus.yml:9    # the only scrape job — points at the API
```

**Impact:** the four flagship metrics — `omnivore_documents_total`, `omnivore_ingest_duration_seconds`, `omnivore_chunks_total`, `omnivore_embeddings_total` — are all emitted **inside the worker processes**. Prometheus never reads them. Therefore:

- Pipeline Overview dashboard:
  - "Document Ingest Rate (per minute)" → **always empty**
  - "Ingest Duration p95 by Handler (s)" → **always empty**
  - "Failed Documents (per minute)" → **always empty**
- Search dashboard:
  - "Embeddings Produced" → **always empty** (embed_chunks runs in the worker)
  - (Search Request Rate and p95 panels work — search runs in the API process.)
- Tenants dashboard:
  - "Documents Indexed per Tenant (last 24h)" → **always empty**
  - "Chunks Produced per Tenant" → **always empty**
  - (Rate-Limit Events 429 panel works — middleware runs in the API process.)

So **7 of the 12 dashboard panels are dead on arrival.** This is the central goal of Phase 5 (correlate a Prometheus spike → trace → log lines for any pipeline bottleneck) and it does not function for the actual pipeline. The API-side `http.server` metrics work, but they tell you nothing about handler latency, document throughput, embeddings cost, or per-tenant pipeline activity — the things the spec was justified by.

**Why no test caught this:** the integration test `test_metrics_endpoint.py` hits the API's `/metrics` endpoint via `ASGITransport` and asserts the metric **names** are present in the body. They are — because the metrics are imported at module load. The test does **not** assert that any counter has non-zero values from worker-side activity, and it has no way to (the worker isn't running in-process).

**Fix (choose one, in increasing order of operational cost):**

1. **Embedded HTTP server in worker (simplest, low overhead):**
   ```python
   # in src/omnivore/worker/tasks.py::on_startup
   from prometheus_client import start_http_server
   start_http_server(port=9101)  # bind 0.0.0.0:9101
   ```
   Then add a second scrape job in `prometheus.yml`:
   ```yaml
   - job_name: omnivore-worker
     static_configs:
       - targets: ["worker:9101"]
   - job_name: omnivore-worker-gpu
     static_configs:
       - targets: ["worker-gpu:9102"]
   ```
   Expose the port in `docker-compose.yml` for worker / worker-gpu. Use distinct ports per worker type to avoid collision when both run on one host. Total surface: ~6 lines of Python + 6 lines of YAML.

2. **Prometheus pushgateway:** workers push to a pushgateway, Prometheus scrapes the gateway. Adds one service to the stack. Right answer when workers are ephemeral (Kubernetes Job-style); overkill here since `worker`, `worker-gpu`, and the ARQ pool itself are long-lived.

3. **prometheus-client multi-process mode (`PROMETHEUS_MULTIPROC_DIR`):** the metrics registry sits on a shared filesystem path; the API's `/metrics` endpoint reads everyone's writes. Requires that the API and worker containers share a writable volume — adds coupling that the current architecture (separate containers, separate Dockerfiles) was built to avoid. Recommend against unless you eliminate per-container isolation.

**My recommendation: option 1.** It matches the rest of the codebase (each service exposes its own surface), needs no shared filesystem, and is testable with a single `curl worker:9101/metrics` smoke test in the existing monitoring playbook. Add a single integration test that runs `_run_ingest` end-to-end (the existing `test_e2e_pipeline.py` already does this) and asserts that after the upload, `DOCUMENTS_TOTAL._value.get()` or `REGISTRY.get_sample_value("omnivore_documents_total", {"status": "indexed", ...})` returns ≥ 1 from the worker process.

This is the **single largest gap in Phase 5** and must be closed before declaring the phase done.

---

## HIGH — should fix before Phase 5 closes

### H-1: GPU worker never calls `setup_tracing`. Every span the GPU pipeline tries to record is silently dropped.

**Where:** `src/omnivore/worker/gpu_main.py:15-24` (`_gpu_on_startup`).

```python
async def _gpu_on_startup(ctx: dict) -> None:
    get_settings()
    registry.discover()
    # ... language detector, NER, lingua warmup ...
    logger.info("gpu_worker.startup", handlers=len(registry.all_handlers()))
```

There is no `setup_tracing(settings)` call. Therefore the OTel global tracer provider in the GPU worker process is `ProxyTracerProvider` (the import-time default), and every `_get_tracer().start_as_current_span(...)` in `gpu_ingest_dispatch` returns a `NonRecordingSpan`. The W3C traceparent is correctly extracted into a context object, but no span is registered with Tempo because no real provider exists.

**Impact:** the spans `omnivore.gpu_ingest.dispatch`, `omnivore.ingest.extract`, `omnivore.ingest.enrich.{language,ner,summarize}`, and `omnivore.embeddings.batch` for audio / video / image OCR documents — by far the slowest and most operationally interesting handlers — **never appear in Tempo**. The "trace_id correlation" goal of Phase 5 is only delivered for the CPU pipeline.

**Fix:** add `setup_tracing(settings)` to `_gpu_on_startup` exactly as `on_startup` does it in `worker/tasks.py:405-416`:

```python
async def _gpu_on_startup(ctx: dict) -> None:
    settings = get_settings()
    setup_tracing(settings)         # ← add this
    # configure_logging(settings)   # ← also needed for H-2 below
    registry.discover()
    ...
```

---

### H-2: Neither worker process calls `configure_logging`. Worker logs aren't JSON, don't carry `trace_id`, and Promtail's JSON parser silently fails on them.

**Where:** `src/omnivore/worker/tasks.py::on_startup` (line 405) and `src/omnivore/worker/gpu_main.py::_gpu_on_startup` (line 15) — neither calls `configure_logging(settings)`. Only the API's `lifespan` (`api/main.py:78`) does.

**Reproduction:**
```bash
$ grep -rn "configure_logging" src/
src/omnivore/logging_config.py:24:def configure_logging(settings: Settings) -> None:
src/omnivore/api/main.py:26:from omnivore.logging_config import configure_logging
src/omnivore/api/main.py:78:    configure_logging(settings)
```

Without `configure_logging`, structlog uses its built-in defaults: `KeyValueRenderer` to stdout, no `_otel_context_processor`, no JSONRenderer. So worker stdout looks like:

```
2026-05-19 12:34:56 [info  ] ingest.dispatch.received document_id=abc handler=pdf
```

…not the JSON Promtail's pipeline expects:

```yaml
pipeline_stages:
  - json:
      expressions:
        level: level
        event: event
        trace_id: trace_id
```

**Impact:**
- Worker log lines in Loki carry no `trace_id` label → Grafana's "Loki → Tempo derived field" correlation does not work for any worker log. You can stare at a Tempo trace and see the span IDs, but the matching log lines in Loki have no `trace_id` to join on.
- The `level` / `event` labels also aren't extracted, so log filtering by structlog event name in Loki doesn't work for workers.
- This compounds with H-1 for the GPU worker: even if you fixed H-1, the GPU worker logs still wouldn't carry `trace_id`.

**Fix:** call `configure_logging(settings)` as the first line of `on_startup` and `_gpu_on_startup`. Order is significant — log it before any other startup log:

```python
async def on_startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings)    # ← FIRST
    setup_tracing(settings)
    registry.discover()
    ...
```

**Why no test caught this:** unit tests import `_otel_context_processor` and call it directly with a manually-configured tracer provider; they never exercise the worker `on_startup` path. There is no integration test that boots an arq worker subprocess and tails its stdout for JSON.

---

### H-3: `_route_template` returns the raw URL path for 404s → **unbounded `route` label cardinality** on `omnivore_http_requests_total`.

**Where:** `src/omnivore/api/main.py:54-59`.

```python
def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path     # ← raw URL when nothing matches
```

The middleware unconditionally records the result as a label value on the counter `omnivore_http_requests_total{method, route, status_code}` and the histogram `omnivore_http_duration_seconds{method, route}`.

**Impact:** any 404 request flips `route` from a bounded template (e.g., `"/v1/documents/{document_id}"`) to the raw URL (e.g., `"/v1/documents/8f7c3e3a-..."`). An attacker (or a misconfigured client) hammering nonexistent paths writes a fresh time series for every distinct URL:

```
omnivore_http_requests_total{method="GET", route="/foo/<random-uuid-1>", status_code="404"} 1
omnivore_http_requests_total{method="GET", route="/foo/<random-uuid-2>", status_code="404"} 1
omnivore_http_requests_total{method="GET", route="/foo/<random-uuid-3>", status_code="404"} 1
...
```

Prometheus's storage degrades quickly past ~100K active series per metric. A single curl loop with `uuidgen` can saturate that in seconds. The same problem applies to the histogram: each unique route gets its own bucket vector, multiplying series count by `len(buckets) + 2`.

**Fix:** when no FastAPI route matches, label as `"__unmatched__"` (or omit the request from metrics entirely):

```python
def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", "__unknown__")
    return "__unmatched__"
```

This caps cardinality at the number of declared FastAPI routes + 1.

---

### H-4: `/metrics` endpoint has no authentication and CORS allows all origins → `tenant_id` label values leak to any caller.

**Where:** `src/omnivore/api/main.py:110-128`.

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
...
@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

The metrics `omnivore_documents_total{tenant_id, ...}`, `omnivore_chunks_total{tenant_id, ...}`, and (less load-bearing) the rate-limit attributable HTTP series are all returned in the response body to any unauthenticated caller.

**Impact:**
- An attacker (or a curious user on `chat.openai.com`) can `fetch("https://api.omnivore.example.com/metrics")` from the browser, observe the response, and enumerate **every tenant UUID** in the system. Tenant IDs are sometimes considered low-sensitivity, but in this product the spec deliberately treats tenant_id as an isolation boundary — leaking the full set undermines that.
- Operational counts (rate of ingest, rate of rate-limit events, etc.) are also exposed, which is useful competitive intelligence even before the tenant-list leak.

**Fix (any one):**
1. Restrict `/metrics` to an internal network — bind the metrics endpoint to a separate port and don't publish it on the docker-compose API service. (Cleanest, but requires running a second FastAPI app or using `--bind` differently.)
2. Require a static bearer token via an `X-Prometheus-Token` header that Prometheus is configured to send.
3. Drop the `tenant_id` label from `DOCUMENTS_TOTAL` and `CHUNKS_TOTAL`, replacing with `tenant_bucket` (a non-reversible hash) if you need per-tenant breakdown but not identification.

Whatever you pick, the change is small. Pick **option 2** for now (easiest to thread through the existing `_PrometheusMiddleware` and add to `prometheus.yml`'s scrape config via `bearer_token_file:`); migrate to option 1 when you ship a real network split.

CORS `allow_origins=["*"]` should also be tightened, but that's a Phase 6 hardening item — the bigger lever is the missing auth on `/metrics` specifically.

---

### H-5: The `http.server` span attribute `tenant_id` named in spec §4 is unfulfilled.

**Where:** spec line 98 declares `http.server | ASGI middleware | http.method, http.route, http.status_code, tenant_id`. Implementation:

- `_PrometheusMiddleware` records metrics but doesn't create OTel spans.
- `FastAPIInstrumentor.instrument_app(app)` creates the `http.server` span automatically — but it has no access to `auth_context` (it runs at the ASGI layer, before FastAPI's dependency injection resolves `Depends(require_auth)`).
- No custom hook attaches `tenant_id` post-resolution.

**Impact:** the Tenants dashboard's "Documents Indexed per Tenant (last 24h)" panel queries `increase(omnivore_documents_total{status="indexed"}[24h])` — that's worker-side metric data, so it depends on C-1 being fixed. But for HTTP-level per-tenant breakdown (e.g., "which tenant is generating these 429s?"), you'd need the http.server span to carry `tenant_id`. It doesn't, so per-tenant HTTP slicing in Tempo is impossible.

**Fix:** after `require_auth` resolves, attach the tenant_id to the current span via an ASGI dependency:

```python
# in src/omnivore/auth/dependencies.py
from opentelemetry import trace as _otel_trace

async def require_auth(...) -> AuthContext:
    ctx = await _resolve(...)
    span = _otel_trace.get_current_span()
    if span.is_recording():
        span.set_attribute("tenant_id", str(ctx.tenant_id))
        span.set_attribute("principal_type", ctx.principal_type)
    return ctx
```

This fires inside the FastAPI-instrumented `http.server` span, so the attribute lands on the right span. Same idea works for `principal_id`, `scopes` (joined as a single string for low cardinality).

---

## MEDIUM

### M-1: Metric cardinality is unbounded as tenant count grows.

`DOCUMENTS_TOTAL{status, tenant_id, mime_type}` and `CHUNKS_TOTAL{kind, tenant_id}` are emitted with `tenant_id` as a UUID. Assume the platform reaches 1,000 tenants × 7 status values × 20 mime types = 140K series for `documents_total` alone, plus the equivalent for chunks_total and (eventually) per-tenant HTTP metrics. Prometheus's recommended ceiling is ~100K series **per scrape target**.

**Resolution path:** at >500 active tenants, replace `tenant_id` with `tenant_tier` (free/standard/enterprise) and move per-tenant breakdown to a separate metric written less frequently (e.g., emit once per minute via an aggregation pass, not per chunk). Track in Phase 6.

### M-2: Spec ↔ implementation divergence: `setup_metrics()` was specified but never implemented.

Spec §3.1 declares `def setup_metrics(settings: Settings) -> None:` as a public function called once from API lifespan and worker `on_startup`. The actual implementation makes metric objects module-level singletons (registered with `prometheus_client.REGISTRY` at import time) — so a `setup_metrics()` function is structurally unnecessary. No harm, but the spec was not updated to reflect the decision, so anyone reading the spec expects a function that does not exist.

**Resolution:** add a noop `def setup_metrics(settings: Settings) -> None: pass` for API compatibility OR strike the function from the spec.

### M-3: Counters and gauges show "no data" until the first observation.

Histograms expose their `_bucket` lines even with zero samples (Prometheus default), but counters and gauges only appear in `/metrics` output after the first `.inc()` / `.set()`. So `omnivore_documents_total{status="failed", ...}` doesn't exist until the first failure — which means the "Failed Documents per minute" panel shows a "no data" placeholder until something fails. Initialize known label combinations to zero on startup:

```python
for status in ("queued", "extracting", "indexed", "failed"):
    DOCUMENTS_TOTAL.labels(status=status, tenant_id="__init__", mime_type="__init__").inc(0)
```

(but discard the "__init__" approach if it complicates cardinality reasoning — the simpler answer is: accept the "no data" UX, document it).

### M-4: `_otel_context_processor` uses `span.is_recording()` instead of `span.get_span_context().is_valid`.

```python
if span.is_recording():
    ctx = span.get_span_context()
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
```

`is_recording()` returns `False` for a `NonRecordingSpan` even when its `SpanContext` is **valid** (e.g., propagated from a parent with a real trace_id but locally we're not recording due to sampling). In that case the log line should still carry the trace_id so it can be correlated with the upstream service's recorded span — but currently it won't.

Today this doesn't matter (everything is `AlwaysOn` sampler), but the moment sampling is enabled the log↔trace correlation degrades silently. Switch to:

```python
ctx = span.get_span_context()
if ctx.is_valid:
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
```

Update `test_otel_context_processor_no_span_is_noop` accordingly.

### M-5: BatchSpanProcessor isn't explicitly shut down on FastAPI lifespan exit.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    ...
    setup_tracing(settings)
    ...
    yield
    poller_task.cancel()
    await _auth_redis.aclose()
    await app.state.arq_pool.close()
    logger.info("shutdown")
    # ← provider.shutdown() never called
```

On graceful shutdown the in-flight span queue may not flush. Add:

```python
from opentelemetry import trace
provider = trace.get_tracer_provider()
if hasattr(provider, "shutdown"):
    provider.shutdown()
```

Same for worker `on_shutdown`.

### M-6: `try: FastAPIInstrumentor.instrument_app(app); except: pass` swallows misconfiguration silently.

```python
try:
    FastAPIInstrumentor.instrument_app(app)
except Exception:
    pass
```

If the OTel-FastAPI integration breaks (version skew, attribute conflict), you lose all `http.server` spans and have no log to investigate. At minimum, log the exception. Better: catch only the specific exceptions that occur in pytest setups (which is presumably why the try/except was added) and let unexpected errors propagate. If the goal was test isolation, gate this on `settings.OTEL_ENABLED` and skip the try/except.

---

## LOW

### L-1: `_route_template` is O(routes) per request.

Iterating `request.app.routes` and calling `route.matches(request.scope)` for every incoming request is acceptable at the current ~30-route count. Once the API grows past ~100 routes consider caching by `(method, path)` after the first resolution or reading the resolved route from `request.scope["route"]` after FastAPI's routing layer fires (would require an outer middleware-after-routing wrapper, which Starlette doesn't natively expose — but `Request.scope.get("route")` is populated after the endpoint returns).

### L-2: Promtail relabel regex hardcodes the project name.

```yaml
- source_labels: [__meta_docker_container_name]
  regex: "/omnidoc-ingest-(.*)-1"
  target_label: service
```

If anyone runs the stack with `COMPOSE_PROJECT_NAME=something-else` or in a directory not named `omnidoc-ingest`, this regex stops matching and the `service` label drops out. Either parameterize via env var or accept the constraint and add a comment.

### L-3: Loki `schema_config.from: "2024-01-01"` is hardcoded.

Anchored to a 2024 schema version. Fine for fresh deployments, awkward for any future schema migration. Track for Phase 6 production sizing.

### L-4: Tempo block retention defaulted to 336h (14d) vs spec'd 48h.

Removing the `compactor:` stanza (to fix the Tempo v3 boot crash documented in the session log) silently increased the default retention from 48h to 336h. Volume usage scales linearly. For the dev profile this is harmless (you'll `docker volume prune` anyway), but the spec's stated retention is wrong. Either restore a Tempo-v3-compatible compactor config (`compactor.compaction.block_retention` is still a valid setting in v3 — the issue was a different removed key — verify and re-add) or update the spec to acknowledge the 14-day default.

### L-5: Loki → Tempo derivedFields regex assumes JSON log lines; breaks in `ENVIRONMENT=development`.

```yaml
matcherRegex: '"trace_id":"([a-f0-9]{32})"'
```

This regex requires the JSON form `"trace_id":"abc123..."`. With `ENVIRONMENT=development`, `configure_logging` uses `ConsoleRenderer`, which prints `trace_id=abc123...`. The derived field doesn't match and the Loki→Tempo "trace this log line" UX breaks in dev. Either:
- Use both regexes: `'(?:"trace_id":"|trace_id=)([a-f0-9]{32})'`
- Or force JSON in dev when monitoring is enabled
- Or document that the correlation only works against production-mode logs

### L-6: `OTEL_EXPORTER_OTLP_ENDPOINT` default doesn't work inside docker-compose.

Default is `http://localhost:4318`, which inside the `api` and `worker` containers points at the container itself — not at the `tempo` service. The `.env` file (not in repo) must override this to `http://tempo:4318` when monitoring is enabled. Add this as an explicit `environment:` block on the `api` / `worker` / `worker-gpu` services in `docker-compose.yml`, gated on `${OTEL_ENABLED:-true}`.

### L-7: Tracer name and service name both default to `"omnivore"`.

`setup_tracing` sets `service.name = settings.OTEL_SERVICE_NAME` (`"omnivore"`); `get_tracer()` defaults to `name="omnivore"`. Tempo will group by service name, which is correct — but the tracer name is meant to identify the **library** producing the span. For our codebase, prefer `get_tracer("omnivore.api")` in API code, `get_tracer("omnivore.worker")` in worker code, etc. Not load-bearing — just easier to filter spans by source library in Tempo's UI.

---

## Verified — no issue

- `db/session.py::reset_guc()` correctly does **rollback → RESET → commit** to survive `pool_reset_on_return="rollback"`. The `test_session_isolation.py` regression tests prove it empirically with `pool_size=1`. ✅
- `_resolve_api_key`, `_resolve_jwt`, `_update_last_used` correctly use `SET LOCAL` inside `async with session.begin()` — the LOCAL scope reverts on commit, no explicit RESET needed. ✅
- `admin_session()` commits on clean exit (Phase 4 C-1 fix held) and resets `bypass_rls` via `reset_guc()`. ✅
- `tenant_session()` resets `current_tenant_id` via `reset_guc()` even after explicit `db.commit()` inside the block. ✅
- The four NEW-H-1 regression tests (`test_raw_set_leaks_across_pool`, `test_set_local_does_not_leak`, `test_admin_session_resets_bypass_rls`, `test_tenant_session_resets_current_tenant_id`) are well-constructed: they force pool reuse with `pool_size=1, max_overflow=0` and assert both the documented Postgres behavior we depend on AND the fix's correctness. ✅
- W3C `traceparent` propagation API → CPU worker: `TraceContextTextMapPropagator().inject({})` on the carrier dict in `documents.py:161-167`; `TraceContextTextMapPropagator().extract({"traceparent": _otel_traceparent})` in `tasks.py:52-56`. Mechanically correct. ✅
- ARQ kwargs carry `_otel_traceparent` as a plain string and round-trip cleanly through the queue. ✅
- `INGEST_DURATION` is observed on **both** success and failure paths (`tasks.py:231-233` and `tasks.py:335-337`). ✅
- `setup_tracing` no-ops gracefully when `OTEL_ENABLED=False` and the BatchSpanProcessor's silent-retry-on-unreachable-Tempo behavior is documented in the docstring. ✅
- `@app.get("/metrics")` correctly works around the Starlette mount-trailing-slash 307 redirect bug. ✅
- The OTEL/Prometheus dependency versions in `pyproject.toml` are compatible (`opentelemetry-sdk>=1.41.1`, `opentelemetry-exporter-otlp-proto-http>=1.41.1`, `opentelemetry-instrumentation-fastapi>=0.62b1`, `prometheus-client>=0.25.0`). ✅
- 346 tests pass; ruff clean across `src/`, `tests/`, `eval/`. ✅
- Grafana `lokiSearch.datasourceUid` and Loki `derivedFields.datasourceUid` cross-reference each other correctly. ✅
- `_PrometheusMiddleware` correctly observes duration in seconds (uses `time.perf_counter()`, observes the diff). ✅
- The "atomic claim" UPDATE-RETURNING pattern for status `queued|routing → extracting` correctly prevents double-processing (Phase 2c fix, still intact). ✅

---

## Recommended next session plan (hand back to Sonnet)

**Goal:** close C-1 + H-1 + H-2 + H-3 + H-4 + H-5. With those six, Phase 5 flips to **PASS** and the phase delivers on the spec's promise (Prometheus spike → trace → log lines, end-to-end through the actual pipeline).

1. **Fix C-1** — embedded metrics HTTP server in workers.
   - Add `start_http_server(9101)` (CPU worker) / `start_http_server(9102)` (GPU worker) inside `on_startup` / `_gpu_on_startup`, after `setup_tracing`.
   - Expose ports `9101` (worker) and `9102` (worker-gpu) in `docker-compose.yml`.
   - Add two scrape jobs in `prometheus.yml` (`omnivore-worker`, `omnivore-worker-gpu`).
   - Add an integration test (extending `test_e2e_pipeline.py`) that asserts `REGISTRY.get_sample_value("omnivore_documents_total", {"status": "indexed", ...})` ≥ 1 after a successful upload — running in the same process as the worker function (the existing test pattern works because `_run_ingest` is called directly).

2. **Fix H-1 + H-2 together** — worker startup polish.
   - Both `on_startup` and `_gpu_on_startup` should call `configure_logging(settings)` then `setup_tracing(settings)` as their first two lines.
   - Add a startup-time log assertion test that boots a worker subprocess, captures stdout, parses one line as JSON, and asserts `trace_id` is present (or absent if no span — that's fine; the schema must parse).

3. **Fix H-3** — bounded route label.
   - One-line change in `_route_template`: return `"__unmatched__"` on the no-match path.
   - Add a unit test: `GET /nonexistent-path` then read `/metrics` and assert that the resulting series has `route="__unmatched__"`.

4. **Fix H-4** — protect `/metrics` from public callers.
   - Add a static bearer token check in the `metrics_endpoint` handler, configured via a new `METRICS_AUTH_TOKEN: SecretStr` setting.
   - Update `prometheus.yml` scrape config to send the token via `authorization.credentials_file:` (mount the token into the Prometheus container as a file).
   - Optionally also drop the `tenant_id` label from `DOCUMENTS_TOTAL` and `CHUNKS_TOTAL` and add a separate `omnivore_tenant_documents_indexed` low-frequency metric — but that's a M-1 sized refactor and can wait.

5. **Fix H-5** — attach `tenant_id` to the FastAPI-instrumented `http.server` span inside `require_auth` (sketch in the H-5 section above).

6. **Defer to Phase 6:** M-1 (cardinality at scale), M-3 (metric warmup UX), L-1 through L-7.

7. **Preflight after fixes:**
   ```bash
   docker compose up -d postgres redis minio
   uv run alembic upgrade head
   ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q   # expect 350+ passed (4-5 new tests)
   uv run ruff check src/ tests/ eval/
   docker compose --profile monitoring up -d
   # Verify the worker metric appears in Prometheus:
   curl -s 'http://localhost:9090/api/v1/query?query=omnivore_documents_total' | jq '.data.result | length'
   # Should be > 0 after a real document upload
   ```

**Acceptance bar for Phase 5 closure:** C-1, H-1, H-2, H-3, H-4 closed (H-5 is strongly recommended but can be deferred to Phase 6 if hard-pressed for time, since its impact is "tenant slicing in Tempo doesn't work" not "no observability at all"). With those five, this audit flips to **PASS**.
