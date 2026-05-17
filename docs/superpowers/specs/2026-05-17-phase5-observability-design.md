# Phase 5 — Observability Design

**Date:** 2026-05-17
**Phase:** 5 of 6
**Estimate:** 1 week
**Status:** Approved for implementation

---

## 1. Goal

Wire full observability into Omnivore so that any bottleneck, error, or latency spike is diagnosable in one place (Grafana) without grepping logs or guessing. Three signals — distributed traces, Prometheus metrics, and structured logs — must be correlated by `trace_id` so you can go from a Prometheus spike → the trace that caused it → the exact log lines for that trace.

---

## 2. Architecture

```
FastAPI / ARQ worker
  │
  ├─ OTel SDK ──────────────────────► Tempo   :4318 (OTLP/HTTP)
  │
  ├─ prometheus-client ──────────────► Prometheus :9090  (scrape /metrics)
  │
  └─ structlog JSON → stdout ────────► Promtail ──► Loki :3100

Grafana :3000
  └─ datasources: Tempo, Prometheus, Loki (auto-provisioned)
  └─ dashboards: Pipeline Overview, Search, Tenants (auto-provisioned)
```

**Correlation:** the OTel SDK injects `trace_id` and `span_id` into the active structlog context via a processor added to `logging_config.py`. Every JSON log line carries these IDs automatically — no changes to existing `logger.info(...)` call-sites.

---

## 3. New Files and Changes

### 3.1 New module: `src/omnivore/observability.py`

Single setup file with two public functions:

```python
def setup_tracing(settings: Settings) -> None:
    """Configure OTel tracer provider with OTLP/HTTP exporter → Tempo.
    No-ops gracefully (logs warning) if Tempo is unreachable at startup.
    """

def setup_metrics(settings: Settings) -> None:
    """Configure prometheus-client registry; expose /metrics endpoint.
    No-ops gracefully if METRICS_ENABLED=False.
    """
```

Called once from `api/main.py` lifespan and `worker/tasks.py::on_startup`.

### 3.2 `src/omnivore/logging_config.py` (modify)

Add a structlog processor after `TimeStamper` that reads the current OTel context and injects `trace_id` and `span_id`:

```python
def _otel_context_processor(logger, method, event_dict):
    span = trace.get_current_span()
    if span.is_recording():
        ctx = span.get_span_context()
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict
```

### 3.3 `src/omnivore/config.py` (modify)

Four new settings, all with safe defaults:

```python
OTEL_ENABLED: bool = True
OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"
OTEL_SERVICE_NAME: str = "omnivore"
METRICS_ENABLED: bool = True
```

### 3.4 `src/omnivore/api/main.py` (modify)

- Call `setup_tracing(settings)` and `setup_metrics(settings)` in the lifespan.
- Add Prometheus ASGI middleware for automatic `omnivore_http_requests_total` and `omnivore_http_duration_seconds` metrics.
- Mount `/metrics` endpoint (Prometheus exposition format).

### 3.5 `src/omnivore/worker/tasks.py` (modify)

- Call `setup_tracing(settings)` and `setup_metrics(settings)` in `on_startup`.
- Wrap `ingest_dispatch`, `gpu_ingest_dispatch`, and `_run_ingest` with OTel spans (context manager, not decorator, to avoid complicating the ARQ task signature).

---

## 4. Spans

| Span name | Created in | Key attributes |
|---|---|---|
| `http.server` | ASGI middleware | `http.method`, `http.route`, `http.status_code`, `tenant_id` |
| `omnivore.ingest.dispatch` | `tasks.py::ingest_dispatch` | `document_id`, `mime_type`, `tenant_id`, `queue` |
| `omnivore.ingest.extract` | `tasks.py::_run_ingest` | `handler_name`, `handler_version` |
| `omnivore.ingest.enrich.language` | `tasks.py::_run_ingest` | `chunk_count` |
| `omnivore.ingest.enrich.ner` | `tasks.py::_run_ingest` | `entity_count` |
| `omnivore.ingest.enrich.summarize` | `tasks.py::_run_ingest` | `model` (if LLM enabled) |
| `omnivore.embeddings.batch` | `pipeline/embeddings.py::embed_chunks` | `chunk_count`, `model` |

Spans propagate context through ARQ jobs using the W3C Trace Context format. When a job is enqueued, the current span's `traceparent` header value is serialized into the ARQ kwargs as `_otel_traceparent`. Worker tasks read this kwarg on entry and call `TraceContextTextMapPropagator().extract()` to restore the parent context, making the worker span a child of the API span. ARQ kwargs are ignored by the actual task function signature (popped before dispatch).

---

## 5. Metrics

| Metric name | Type | Labels | Incremented in |
|---|---|---|---|
| `omnivore_documents_total` | Counter | `status`, `tenant_id`, `mime_type` | After status transition in `tasks.py` |
| `omnivore_ingest_duration_seconds` | Histogram | `handler`, `status` | End of `_run_ingest` |
| `omnivore_chunks_total` | Counter | `kind`, `tenant_id` | After chunk persistence |
| `omnivore_embeddings_total` | Counter | `model` | After `embed_chunks` |
| `omnivore_search_duration_seconds` | Histogram | `mode` | After search query resolves |
| `omnivore_queue_depth` | Gauge | `queue` | Polled every 30s via `asyncio.create_task` loop started in API lifespan; reads ARQ Redis `zcard` |
| `omnivore_http_requests_total` | Counter | `method`, `route`, `status_code` | ASGI middleware (auto) |
| `omnivore_http_duration_seconds` | Histogram | `method`, `route` | ASGI middleware (auto) |

Bucket boundaries for histograms:
- `ingest_duration_seconds`: `[0.1, 0.5, 1, 5, 10, 30, 60, 120, 300]`
- `search_duration_seconds`: `[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2]`
- `http_duration_seconds`: default Prometheus buckets

---

## 6. Infrastructure — docker-compose.yml additions

All five services are gated behind `profiles: [monitoring]`. Run with:
```bash
docker compose --profile monitoring up -d
```

| Service | Image | Exposed port | Data volume |
|---|---|---|---|
| `tempo` | `grafana/tempo:latest` | 4318 (OTLP) | `tempo_data` |
| `prometheus` | `prom/prometheus:latest` | 9090 | `prometheus_data` |
| `loki` | `grafana/loki:latest` | 3100 | `loki_data` |
| `promtail` | `grafana/promtail:latest` | — (reads Docker socket) | — |
| `grafana` | `grafana/grafana:latest` | 3000 | `grafana_data` |

### Config files (`observability/`)

```
observability/
  tempo.yaml                     # local backend, OTLP receiver
  prometheus.yml                 # scrape omnivore API /metrics every 15s
  loki-config.yaml               # local filesystem storage
  promtail-config.yaml           # Docker log driver → Loki, parse JSON
  grafana/
    provisioning/
      datasources/
        datasources.yaml         # auto-provision Tempo, Prometheus, Loki
      dashboards/
        dashboards.yaml          # load from /dashboards dir
    dashboards/
      pipeline-overview.json     # ingest rate, p95 latency, queue depth, error rate
      search.json                # search rate, p95 by mode
      tenants.json               # per-tenant document counts, rate-limit events
```

Grafana runs with `GF_AUTH_ANONYMOUS_ENABLED=true` and `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin` for local dev — no login required.

---

## 7. Grafana Dashboards

### 7.1 Pipeline Overview

- **Ingest rate** — `rate(omnivore_documents_total[5m])` by status
- **Ingest latency (p50 / p95 / p99)** — histogram_quantile on `omnivore_ingest_duration_seconds` by handler
- **Queue depth** — `omnivore_queue_depth` by queue (cpu/gpu/default)
- **Error rate** — `rate(omnivore_documents_total{status="failed"}[5m])`
- **Active tenants** — count_values on tenant_id label (last 1h)

### 7.2 Search

- **Search request rate** by mode
- **Search p95 latency** by mode
- **Search error rate**

### 7.3 Tenants

- **Documents indexed per tenant** (last 24h)
- **Rate-limit events** (`omnivore_http_requests_total{status_code="429"}`)

---

## 8. Testing

### 8.1 Unit tests (`tests/unit/test_observability.py`)

- `setup_tracing()` with `InMemorySpanExporter` → spans have expected names and attributes
- `setup_metrics()` → metric names registered in the Prometheus registry
- `_otel_context_processor` → injects `trace_id`/`span_id` when a span is active, skips them when not

### 8.2 Integration tests (`tests/integration/test_observability_integration.py`)

- `GET /metrics` returns 200 with `text/plain` content type and contains `omnivore_http_requests_total`
- Upload a document → assert `omnivore_documents_total{status="queued"}` counter incremented
- These run against the ASGI app with `ASGITransport` (same pattern as existing integration tests)

**No E2E tests** against Tempo/Loki/Grafana containers — those are not in CI. The above covers correctness; dashboards are verified manually.

### 8.3 Coverage

New files (`observability.py`, trace context processor, metrics middleware) must reach ≥80% alongside the existing suite.

---

## 9. Dependencies

New packages (added via `uv add`):

```
opentelemetry-sdk
opentelemetry-exporter-otlp-proto-http
opentelemetry-instrumentation-fastapi   # ASGI middleware span
prometheus-client
starlette-prometheus                    # or custom ASGI middleware
```

---

## 10. Out of Scope for Phase 5

- Sentry exception reporting (deferred — Phase 6 hardening)
- OTel Collector sidecar (deferred — add when fan-out routing or sampling needed)
- Alerting rules in Prometheus / Alertmanager (deferred — Phase 6)
- Production Loki/Tempo sizing and retention policies (deferred — Phase 6)
- Per-tenant cost metrics and dashboards (deferred — architecture §9 row 12)
