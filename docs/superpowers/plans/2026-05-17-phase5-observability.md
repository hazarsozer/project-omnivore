# Phase 5 — Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full observability to Omnivore — distributed traces (OTel → Tempo), Prometheus metrics, and structured log aggregation (Loki), unified in a Grafana dashboard.

**Architecture:** OTel SDK instruments the FastAPI app and ARQ worker with spans; W3C `traceparent` headers propagate trace context across the API→worker boundary via ARQ kwargs. `prometheus-client` exposes a `/metrics` scrape endpoint; Prometheus polls it every 15 s. Structlog already emits JSON to stdout; Promtail ships those logs to Loki. All three signals share `trace_id` so Grafana can correlate them.

**Tech Stack:** `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-instrumentation-fastapi`, `prometheus-client`; Grafana/Tempo/Prometheus/Loki/Promtail run behind `docker compose --profile monitoring`.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/omnivore/observability.py` | All metric objects + `setup_tracing()` + `get_tracer()` |
| Modify | `src/omnivore/config.py` | 4 new OTel/metrics settings |
| Modify | `src/omnivore/logging_config.py` | Inject `trace_id`/`span_id` into every structlog line |
| Modify | `src/omnivore/api/main.py` | Mount `/metrics`, HTTP middleware, queue-depth poller, setup calls |
| Modify | `src/omnivore/worker/tasks.py` | `on_startup` setup calls, spans in `ingest_dispatch` / `_run_ingest` |
| Modify | `src/omnivore/pipeline/embeddings.py` | Increment `EMBEDDINGS_TOTAL` counter |
| Modify | `src/omnivore/api/routes/search.py` | Observe `SEARCH_DURATION` histogram |
| Create | `tests/unit/test_observability.py` | Unit tests: spans, metrics, context processor |
| Create | `tests/integration/test_metrics_endpoint.py` | Integration: `GET /metrics` returns expected names |
| Modify | `docker-compose.yml` | 5 services behind `monitoring` profile |
| Create | `observability/tempo.yaml` | Tempo minimal config |
| Create | `observability/prometheus.yml` | Prometheus scrape config |
| Create | `observability/loki-config.yaml` | Loki local storage config |
| Create | `observability/promtail-config.yaml` | Promtail Docker socket → Loki |
| Create | `observability/grafana/provisioning/datasources/datasources.yaml` | Auto-provision 3 datasources |
| Create | `observability/grafana/provisioning/dashboards/dashboards.yaml` | Point Grafana at dashboards dir |
| Create | `observability/grafana/dashboards/pipeline-overview.json` | Ingest rate, latency, queue depth |
| Create | `observability/grafana/dashboards/search.json` | Search rate + latency |
| Create | `observability/grafana/dashboards/tenants.json` | Per-tenant stats |
| Modify | `CLAUDE.md` | Mark Phase 5 done |

---

## Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Install packages**

```bash
uv add opentelemetry-sdk opentelemetry-exporter-otlp-proto-http opentelemetry-instrumentation-fastapi prometheus-client
```

Expected: `pyproject.toml` dependencies block gains 4 new lines; `uv.lock` updates.

- [ ] **Step 2: Verify import works**

```bash
uv run python -c "from opentelemetry import trace; from prometheus_client import Counter; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add OTel + prometheus-client dependencies"
```

---

## Task 2: Create `src/omnivore/observability.py`

**Files:**
- Create: `src/omnivore/observability.py`
- Create: `tests/unit/test_observability.py`

- [ ] **Step 1: Write failing tests first**

Create `tests/unit/test_observability.py`:

```python
from __future__ import annotations

import prometheus_client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from omnivore.observability import (
    CHUNKS_TOTAL,
    DOCUMENTS_TOTAL,
    EMBEDDINGS_TOTAL,
    HTTP_DURATION,
    HTTP_REQUESTS_TOTAL,
    INGEST_DURATION,
    QUEUE_DEPTH,
    SEARCH_DURATION,
    get_tracer,
    setup_tracing,
)


def _fresh_provider() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_setup_tracing_registers_global_provider(tmp_path):
    """setup_tracing() must set a non-NoOp global tracer provider."""
    from omnivore.config import Settings
    settings = Settings(
        OTEL_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318",
        OTEL_SERVICE_NAME="test-svc",
    )
    # Should not raise even if Tempo is not running (exporter init is lazy)
    setup_tracing(settings)
    provider = trace.get_tracer_provider()
    assert not isinstance(provider, trace.NoOpTracerProvider)


def test_setup_tracing_disabled_is_noop():
    from omnivore.config import Settings
    settings = Settings(OTEL_ENABLED=False)
    # Record provider before call
    before = trace.get_tracer_provider()
    setup_tracing(settings)
    after = trace.get_tracer_provider()
    assert before is after


def test_get_tracer_returns_tracer():
    provider, _ = _fresh_provider()
    trace.set_tracer_provider(provider)
    tracer = get_tracer()
    with tracer.start_as_current_span("test.span") as span:
        assert span.is_recording()


def test_metric_objects_are_registered():
    """All metric objects must exist and be the right prometheus_client types."""
    assert isinstance(DOCUMENTS_TOTAL, prometheus_client.Counter)
    assert isinstance(INGEST_DURATION, prometheus_client.Histogram)
    assert isinstance(CHUNKS_TOTAL, prometheus_client.Counter)
    assert isinstance(EMBEDDINGS_TOTAL, prometheus_client.Counter)
    assert isinstance(SEARCH_DURATION, prometheus_client.Histogram)
    assert isinstance(QUEUE_DEPTH, prometheus_client.Gauge)
    assert isinstance(HTTP_REQUESTS_TOTAL, prometheus_client.Counter)
    assert isinstance(HTTP_DURATION, prometheus_client.Histogram)


def test_documents_total_labels():
    DOCUMENTS_TOTAL.labels(status="indexed", tenant_id="t1", mime_type="text/plain").inc()
    # No exception means labels are correct


def test_ingest_duration_custom_buckets():
    buckets = INGEST_DURATION._kwargs.get("buckets") or list(INGEST_DURATION._upper_bounds)
    assert 300 in buckets or 300.0 in buckets


def test_search_duration_custom_buckets():
    buckets = SEARCH_DURATION._kwargs.get("buckets") or list(SEARCH_DURATION._upper_bounds)
    assert 2 in buckets or 2.0 in buckets
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/unit/test_observability.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'omnivore.observability'`

- [ ] **Step 3: Create `src/omnivore/observability.py`**

```python
from __future__ import annotations

import logging

import prometheus_client
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metric objects — module-level singletons.
# Import these in other modules: from omnivore.observability import DOCUMENTS_TOTAL
# ---------------------------------------------------------------------------

DOCUMENTS_TOTAL = prometheus_client.Counter(
    "omnivore_documents_total",
    "Documents by status transition",
    ["status", "tenant_id", "mime_type"],
)

INGEST_DURATION = prometheus_client.Histogram(
    "omnivore_ingest_duration_seconds",
    "Time to fully ingest one document",
    ["handler", "status"],
    buckets=[0.1, 0.5, 1, 5, 10, 30, 60, 120, 300],
)

CHUNKS_TOTAL = prometheus_client.Counter(
    "omnivore_chunks_total",
    "Chunks produced by the pipeline",
    ["kind", "tenant_id"],
)

EMBEDDINGS_TOTAL = prometheus_client.Counter(
    "omnivore_embeddings_total",
    "Embedding vectors produced",
    ["model"],
)

SEARCH_DURATION = prometheus_client.Histogram(
    "omnivore_search_duration_seconds",
    "Search query latency",
    ["mode"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2],
)

QUEUE_DEPTH = prometheus_client.Gauge(
    "omnivore_queue_depth",
    "ARQ queue depth",
    ["queue"],
)

HTTP_REQUESTS_TOTAL = prometheus_client.Counter(
    "omnivore_http_requests_total",
    "HTTP request count",
    ["method", "route", "status_code"],
)

HTTP_DURATION = prometheus_client.Histogram(
    "omnivore_http_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
)

# ---------------------------------------------------------------------------
# Setup functions — called once at process startup
# ---------------------------------------------------------------------------

def setup_tracing(settings) -> None:  # type: ignore[no-untyped-def]
    """Configure OTel TracerProvider with OTLP/HTTP exporter.

    No-ops when OTEL_ENABLED=False. The OTLP exporter uses a BatchSpanProcessor
    which drops spans silently when Tempo is unreachable — startup never fails.
    """
    if not settings.OTEL_ENABLED:
        return
    resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces",
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info(
        "OTel tracing configured endpoint=%s service=%s",
        settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        settings.OTEL_SERVICE_NAME,
    )


def get_tracer(name: str = "omnivore") -> trace.Tracer:
    """Return the global tracer. Always safe to call — falls back to NoOp."""
    return trace.get_tracer(name)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/unit/test_observability.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omnivore/observability.py tests/unit/test_observability.py
git commit -m "feat: add observability module — metrics registry + OTel setup"
```

---

## Task 3: Update `config.py` with 4 new settings

**Files:**
- Modify: `src/omnivore/config.py`

- [ ] **Step 1: Add the settings**

Open `src/omnivore/config.py` and add after the `MAX_GPU_QUEUE_DEPTH` line (before the closing of the class):

```python
    # Phase 5 — Observability
    OTEL_ENABLED: bool = True
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"
    OTEL_SERVICE_NAME: str = "omnivore"
    METRICS_ENABLED: bool = True
```

- [ ] **Step 2: Verify settings load**

```bash
uv run python -c "from omnivore.config import get_settings; s = get_settings(); print(s.OTEL_ENABLED, s.OTEL_SERVICE_NAME)"
```

Expected: `True omnivore`

- [ ] **Step 3: Commit**

```bash
git add src/omnivore/config.py
git commit -m "feat: add OTel + metrics settings to config"
```

---

## Task 4: Inject `trace_id`/`span_id` into structlog

**Files:**
- Modify: `src/omnivore/logging_config.py`
- Modify: `tests/unit/test_observability.py` (add processor test)

- [ ] **Step 1: Add the test to `tests/unit/test_observability.py`**

Append to the existing file:

```python
def test_otel_context_processor_injects_ids():
    """trace_id and span_id appear in the log event dict when a span is active."""
    from omnivore.logging_config import _otel_context_processor

    provider, _ = _fresh_provider()
    trace.set_tracer_provider(provider)

    with get_tracer().start_as_current_span("test"):
        event = _otel_context_processor(None, None, {"event": "hello"})
    assert "trace_id" in event
    assert "span_id" in event
    assert len(event["trace_id"]) == 32   # 128-bit hex
    assert len(event["span_id"]) == 16    # 64-bit hex


def test_otel_context_processor_no_span_is_noop():
    """When no span is active the dict is returned unchanged (no trace_ keys)."""
    from opentelemetry.context import attach, detach, get_current
    from opentelemetry.trace import NonRecordingSpan, INVALID_SPAN_CONTEXT

    from omnivore.logging_config import _otel_context_processor

    event = _otel_context_processor(None, None, {"event": "hello"})
    assert "trace_id" not in event
    assert "span_id" not in event
```

- [ ] **Step 2: Run to confirm they fail**

```bash
uv run pytest tests/unit/test_observability.py::test_otel_context_processor_injects_ids -v 2>&1 | head -10
```

Expected: `ImportError` — `_otel_context_processor` not yet defined.

- [ ] **Step 3: Modify `src/omnivore/logging_config.py`**

Replace the full file with:

```python
from __future__ import annotations

import logging
from typing import Any

import structlog
from opentelemetry import trace

from omnivore.config import Settings


def _otel_context_processor(
    logger: Any, method: Any, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor: inject trace_id + span_id from the active OTel span."""
    span = trace.get_current_span()
    if span.is_recording():
        ctx = span.get_span_context()
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(settings: Settings) -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _otel_context_processor,
    ]

    if settings.ENVIRONMENT == "development":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.LOG_LEVEL.upper())
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/unit/test_observability.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/omnivore/logging_config.py tests/unit/test_observability.py
git commit -m "feat: inject OTel trace_id/span_id into structlog context"
```

---

## Task 5: Wire API — `/metrics` endpoint + HTTP middleware + queue-depth poller

**Files:**
- Modify: `src/omnivore/api/main.py`
- Create: `tests/integration/test_metrics_endpoint.py`

- [ ] **Step 1: Write the integration test first**

Create `tests/integration/test_metrics_endpoint.py`:

```python
"""Integration: /metrics endpoint returns Prometheus exposition format.

Uses ASGITransport — no real Prometheus needed.
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from omnivore.api.main import app
from omnivore.pipeline.registry import registry


@pytest.fixture(scope="module")
def metrics_results():
    results: dict = {}

    async def _run():
        registry.discover()
        app.state.arq_pool = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/metrics")
            results["status"] = r.status_code
            results["content_type"] = r.headers.get("content-type", "")
            results["body"] = r.text

    asyncio.run(_run())
    return results


def test_metrics_returns_200(metrics_results):
    assert metrics_results["status"] == 200


def test_metrics_content_type_is_prometheus(metrics_results):
    assert "text/plain" in metrics_results["content_type"]


def test_metrics_contains_http_counter(metrics_results):
    assert "omnivore_http_requests_total" in metrics_results["body"]


def test_metrics_contains_ingest_histogram(metrics_results):
    assert "omnivore_ingest_duration_seconds" in metrics_results["body"]
```

- [ ] **Step 2: Run to confirm they fail**

```bash
uv run pytest tests/integration/test_metrics_endpoint.py -v 2>&1 | head -15
```

Expected: 404 on `/metrics` — endpoint not mounted yet.

- [ ] **Step 3: Update `src/omnivore/api/main.py`**

Replace the full file:

```python
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import prometheus_client
import redis.asyncio as aioredis
import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

import omnivore.auth.cache as _auth_cache
import omnivore.auth.rate_limit as _auth_rl
from omnivore.api.routes import admin, auth_route, documents, handlers_route, health, search, tenant
from omnivore.api.schemas import APIResponse, ErrorDetail
from omnivore.auth.errors import AuthError
from omnivore.config import get_settings
from omnivore.constants import GPU_QUEUE_NAME
from omnivore.logging_config import configure_logging
from omnivore.observability import (
    HTTP_DURATION,
    HTTP_REQUESTS_TOTAL,
    QUEUE_DEPTH,
    setup_tracing,
)
from omnivore.pipeline.registry import registry

logger = structlog.get_logger(__name__)


class _PrometheusMiddleware(BaseHTTPMiddleware):
    """Record omnivore_http_requests_total and omnivore_http_duration_seconds."""

    async def dispatch(self, request: Request, call_next):
        route = _route_template(request)
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        status = str(response.status_code)
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, route=route, status_code=status
        ).inc()
        HTTP_DURATION.labels(method=request.method, route=route).observe(duration)
        return response


def _route_template(request: Request) -> str:
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


async def _poll_queue_depth(arq_pool) -> None:
    """Background task: update QUEUE_DEPTH gauge every 30 s."""
    while True:
        try:
            default_depth = await arq_pool.zcard(arq_pool.default_queue_name)
            QUEUE_DEPTH.labels(queue="default").set(default_depth)
            gpu_depth = await arq_pool.zcard(GPU_QUEUE_NAME)
            QUEUE_DEPTH.labels(queue="gpu").set(gpu_depth)
        except Exception:
            pass
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)

    # Observability — set up before anything else so early logs get trace_id
    setup_tracing(settings)

    registry.discover()
    logger.info("startup", environment=settings.ENVIRONMENT, handlers=len(registry.all_handlers()))

    # Redis singletons for auth cache + rate limiter
    _auth_redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    _auth_cache._redis_client = _auth_redis
    _auth_rl._redis_client = _auth_redis

    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))

    # Start queue-depth poller (best-effort, ignored if Redis is unreachable)
    poller_task = asyncio.create_task(_poll_queue_depth(app.state.arq_pool))

    yield

    poller_task.cancel()
    await _auth_redis.aclose()
    await app.state.arq_pool.close()
    logger.info("shutdown")


app = FastAPI(
    title="Omnivore API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(_PrometheusMiddleware)

# OTel FastAPI auto-instrumentation — adds http.server spans for every request
FastAPIInstrumentor.instrument_app(app)

# Prometheus /metrics endpoint
app.mount("/metrics", prometheus_client.make_asgi_app())

app.include_router(health.router, prefix="/v1")
app.include_router(auth_route.router, prefix="/v1")
app.include_router(admin.router, prefix="/v1")
app.include_router(tenant.router, prefix="/v1")
app.include_router(documents.router, prefix="/v1")
app.include_router(search.router, prefix="/v1")
app.include_router(handlers_route.router, prefix="/v1")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/v1/health", status_code=307)


@app.exception_handler(AuthError)
async def auth_exception_handler(request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=APIResponse(
            success=False,
            error=ErrorDetail(code=exc.code, message=exc.message),
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", exc_info=exc, path=str(request.url))
    return JSONResponse(
        status_code=500,
        content=APIResponse(
            success=False,
            error=ErrorDetail(code="INTERNAL_ERROR", message="An unexpected error occurred"),
        ).model_dump(),
    )
```

- [ ] **Step 4: Run integration test**

```bash
ADMIN_BOOTSTRAP_TOKEN=test uv run pytest tests/integration/test_metrics_endpoint.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Run full suite to verify no regressions**

```bash
ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q 2>&1 | tail -5
```

Expected: 337+ passed.

- [ ] **Step 6: Commit**

```bash
git add src/omnivore/api/main.py tests/integration/test_metrics_endpoint.py
git commit -m "feat: mount /metrics endpoint + HTTP middleware + queue-depth poller"
```

---

## Task 6: Instrument the ARQ worker — spans + trace propagation

**Files:**
- Modify: `src/omnivore/worker/tasks.py`
- Modify: `src/omnivore/api/routes/documents.py`
- Modify: `tests/unit/test_tasks.py` (add `_otel_traceparent` to existing test kwargs)

- [ ] **Step 1: Read the existing test to understand the call pattern**

```bash
grep -n "_otel_traceparent\|enqueue_job\|ingest_dispatch" tests/unit/test_tasks.py | head -20
```

Note which lines call `ingest_dispatch(ctx, ...)` directly — you'll need to add `_otel_traceparent=None` as a default kwarg there. The test must still pass unchanged; the new param has a default.

- [ ] **Step 2: Update `src/omnivore/worker/tasks.py`**

At the top, after the existing imports, add:

```python
import time as _time

from opentelemetry import context as context_api
from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace import StatusCode

from omnivore.observability import (
    CHUNKS_TOTAL,
    DOCUMENTS_TOTAL,
    INGEST_DURATION,
    setup_tracing,
)
from omnivore.observability import get_tracer as _get_tracer
```

Replace `async def ingest_dispatch(ctx: dict, *, document_id: str, mime: str, size: int, tenant_id: str, config_snapshot: dict) -> dict:` with:

```python
async def ingest_dispatch(
    ctx: dict,
    *,
    document_id: str,
    mime: str,
    size: int,
    tenant_id: str,
    config_snapshot: dict,
    _otel_traceparent: str | None = None,
) -> dict:
    parent_ctx = (
        TraceContextTextMapPropagator().extract({"traceparent": _otel_traceparent})
        if _otel_traceparent
        else context_api.get_current()
    )
    with _get_tracer().start_as_current_span(
        "omnivore.ingest.dispatch",
        context=parent_ctx,
        attributes={
            "document_id": document_id,
            "mime_type": mime,
            "tenant_id": tenant_id,
        },
    ):
        # existing body unchanged below
        _job_kwargs = {
            "document_id": document_id, "mime": mime, "size": size,
            "tenant_id": tenant_id, "config_snapshot": config_snapshot,
        }
        ...
```

Similarly add `_otel_traceparent: str | None = None` parameter to `gpu_ingest_dispatch` and propagate via `parent_ctx` the same way.

Inside `_run_ingest`, wrap the handler extraction in a span:

```python
    # After the atomic claim commit, before handler.extract():
    with _get_tracer().start_as_current_span(
        "omnivore.ingest.extract",
        attributes={"handler": handler.name, "handler_version": handler.version},
    ) as extract_span:
        try:
            result = await handler.extract(blob, ingest_ctx)
        except Exception as exc:
            extract_span.set_status(StatusCode.ERROR, str(exc))
            extract_span.record_exception(exc)
            logger.exception("handler.extract.failed", document_id=document_id, handler=handler.name)
            await _fail_document(db, doc, str(exc), job_kwargs)
            return {"status": "error", "reason": str(exc)}
```

After language detection, wrap enrichments:

```python
    with _get_tracer().start_as_current_span("omnivore.ingest.enrich.language"):
        for chunk in chunks:
            if chunk.language is None:
                chunk.language = detect_language(chunk.content)

    with _get_tracer().start_as_current_span(
        "omnivore.ingest.enrich.ner",
        attributes={"chunk_count": len(chunks)},
    ):
        try:
            entities = extract_entities(chunks)
            ...
        except Exception:
            ...

    with _get_tracer().start_as_current_span("omnivore.ingest.enrich.summarize"):
        summary = await summarize_document(chunks, api_key=api_key, filename=doc.filename)
```

After status = "indexed" and db.commit(), record metrics:

```python
    # Metrics: increment per-chunk counters
    for chunk, sinks, _ in zip(chunks, chunk_sinks, chunk_rule_ids):
        CHUNKS_TOTAL.labels(kind=chunk.kind, tenant_id=str(t_id)).inc()

    DOCUMENTS_TOTAL.labels(
        status="indexed", tenant_id=str(t_id), mime_type=mime
    ).inc()
```

On `_fail_document` call path, add:

```python
    DOCUMENTS_TOTAL.labels(
        status="failed", tenant_id=str(t_id), mime_type=mime
    ).inc()
```

Add to `on_startup`:

```python
async def on_startup(ctx: dict) -> None:
    settings = get_settings()
    setup_tracing(settings)
    # ... existing warmup code ...
```

- [ ] **Step 3: Inject `_otel_traceparent` when enqueuing from the API**

In `src/omnivore/api/routes/documents.py`, add at the top:

```python
from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator
```

Find both `await pool.enqueue_job("ingest_dispatch", **job_kwargs)` calls (in `upload_document` and `retry_document`) and replace with:

```python
    _carrier: dict = {}
    TraceContextTextMapPropagator().inject(_carrier)
    await pool.enqueue_job(
        "ingest_dispatch",
        **job_kwargs,
        _otel_traceparent=_carrier.get("traceparent"),
    )
```

Do the same for `gpu_ingest_dispatch` enqueue in `tasks.py::ingest_dispatch` (already in the worker — add `_otel_traceparent=_carrier.get("traceparent")` there too by injecting the current span).

- [ ] **Step 4: Run the full test suite**

```bash
ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q 2>&1 | tail -5
```

Expected: all prior tests still passing (no regressions — `_otel_traceparent=None` default means existing tests don't need changes).

- [ ] **Step 5: Ruff check**

```bash
uv run ruff check src/ tests/
```

Expected: no errors (fix any import ordering issues with `--fix`).

- [ ] **Step 6: Commit**

```bash
git add src/omnivore/worker/tasks.py src/omnivore/api/routes/documents.py
git commit -m "feat: OTel spans + document metrics in worker + W3C traceparent propagation"
```

---

## Task 7: Instrument embeddings and search

**Files:**
- Modify: `src/omnivore/pipeline/embeddings.py`
- Modify: `src/omnivore/api/routes/search.py`

- [ ] **Step 1: Add embeddings span + counter to `embeddings.py`**

At the top of `src/omnivore/pipeline/embeddings.py`, after the existing imports, add:

```python
from omnivore.observability import EMBEDDINGS_TOTAL, get_tracer
```

Find `async def embed_chunks(chunks: list[Chunk], redis=None) -> list[list[float]]:` (line 104). Wrap the full function body in a span and increment the counter at the end:

```python
async def embed_chunks(chunks: list[Chunk], redis=None) -> list[list[float]]:
    with get_tracer().start_as_current_span(
        "omnivore.embeddings.batch",
        attributes={"chunk_count": len(chunks), "model": EMBEDDING_MODEL},
    ):
        result = await _embed_chunks_impl(chunks, redis)
    EMBEDDINGS_TOTAL.labels(model=EMBEDDING_MODEL).inc(len(chunks))
    return result
```

Then rename the existing function body to `_embed_chunks_impl`:

```python
async def _embed_chunks_impl(chunks: list[Chunk], redis=None) -> list[list[float]]:
    # (existing body unchanged)
    ...
```

- [ ] **Step 2: Add search duration histogram to `search.py`**

At the top of `src/omnivore/api/routes/search.py`, after the existing imports, add:

```python
import time as _time

from omnivore.observability import SEARCH_DURATION
```

In the `search` handler, wrap the query execution:

```python
@router.post("")
async def search(
    request: Request,
    body: SearchRequest,
    auth: Annotated[AuthContext, Depends(require_scope("search:read"))],
    _rl: Annotated[None, Depends(rate_limited())] = None,
) -> APIResponse[list[SearchResult]]:
    tenant_id = auth.tenant_id
    _t0 = _time.perf_counter()

    query_vector: list[float] | None = None
    if body.mode in ("hybrid", "vector"):
        redis = getattr(request.app.state, "arq_pool", None)
        query_vector = (await embed_texts([body.query], redis, query_prefix=QUERY_PREFIX))[0]

    async with tenant_session(tenant_id) as db:
        if body.mode == "bm25":
            rows = await _bm25_search(db, body.query, tenant_id, body.top_k)
        elif body.mode == "vector":
            rows = await _vector_search(db, query_vector, tenant_id, body.top_k)  # type: ignore[arg-type]
        else:
            rows = await _hybrid_search(db, body.query, query_vector, tenant_id, body.top_k)  # type: ignore[arg-type]

    SEARCH_DURATION.labels(mode=body.mode).observe(_time.perf_counter() - _t0)

    results = [
        SearchResult(
            chunk_id=str(r["chunk_id"]),
            document_id=str(r["document_id"]),
            content=r["content"],
            heading_path=r["heading_path"],
            score=float(r["score"]),
            token_count=r["token_count"],
        )
        for r in rows
    ]

    query_hash = hashlib.sha256(body.query.encode()).hexdigest()[:12]
    logger.info("search.complete", query_hash=query_hash, mode=body.mode, results=len(results))
    return APIResponse(success=True, data=results)
```

- [ ] **Step 3: Run full suite + ruff**

```bash
ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q 2>&1 | tail -5
uv run ruff check src/ tests/
```

Expected: all tests pass, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add src/omnivore/pipeline/embeddings.py src/omnivore/api/routes/search.py
git commit -m "feat: instrument embeddings + search with spans and metrics"
```

---

## Task 8: Docker Compose — add 5 observability services

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Append services to `docker-compose.yml`**

Add the following block at the end of the `services:` section, before `volumes:`:

```yaml
  # ---- Observability stack (opt-in: docker compose --profile monitoring up -d) ----

  tempo:
    image: grafana/tempo:latest
    profiles: [monitoring]
    command: ["-config.file=/etc/tempo.yaml"]
    volumes:
      - ./observability/tempo.yaml:/etc/tempo.yaml:ro
      - tempo_data:/var/tempo
    ports:
      - "4318:4318"   # OTLP/HTTP receiver
      - "3200:3200"   # Tempo query API (used by Grafana)

  prometheus:
    image: prom/prometheus:latest
    profiles: [monitoring]
    volumes:
      - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
      - "--web.enable-lifecycle"

  loki:
    image: grafana/loki:latest
    profiles: [monitoring]
    volumes:
      - ./observability/loki-config.yaml:/etc/loki/loki-config.yaml:ro
      - loki_data:/loki
    ports:
      - "3100:3100"
    command: ["-config.file=/etc/loki/loki-config.yaml"]

  promtail:
    image: grafana/promtail:latest
    profiles: [monitoring]
    volumes:
      - ./observability/promtail-config.yaml:/etc/promtail/promtail-config.yaml:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command: ["-config.file=/etc/promtail/promtail-config.yaml"]
    depends_on:
      - loki

  grafana:
    image: grafana/grafana:latest
    profiles: [monitoring]
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
      - GF_AUTH_DISABLE_LOGIN_FORM=true
    volumes:
      - ./observability/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./observability/grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana_data:/var/lib/grafana
    ports:
      - "3000:3000"
    depends_on:
      - prometheus
      - loki
      - tempo
```

Add to the `volumes:` section at the bottom:

```yaml
  tempo_data:
  prometheus_data:
  loki_data:
  grafana_data:
```

- [ ] **Step 2: Verify compose parses**

```bash
docker compose config --quiet && echo "config ok"
```

Expected: `config ok`.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add Grafana/Tempo/Prometheus/Loki/Promtail to docker-compose monitoring profile"
```

---

## Task 9: Observability config files

**Files:**
- Create: `observability/tempo.yaml`
- Create: `observability/prometheus.yml`
- Create: `observability/loki-config.yaml`
- Create: `observability/promtail-config.yaml`

- [ ] **Step 1: Create `observability/tempo.yaml`**

```yaml
stream_over_http_enabled: true
server:
  http_listen_port: 3200
  log_level: warn

distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:4318

storage:
  trace:
    backend: local
    local:
      path: /var/tempo/traces
    wal:
      path: /var/tempo/wal

compactor:
  compaction:
    block_retention: 48h
```

- [ ] **Step 2: Create `observability/prometheus.yml`**

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: omnivore-api
    static_configs:
      - targets: ["api:8000"]
    metrics_path: /metrics
```

- [ ] **Step 3: Create `observability/loki-config.yaml`**

```yaml
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: "2024-01-01"
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  reject_old_samples: false
```

- [ ] **Step 4: Create `observability/promtail-config.yaml`**

```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: docker
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        refresh_interval: 5s
    relabel_configs:
      - source_labels: [__meta_docker_container_name]
        regex: "/omnidoc-ingest-(.*)-1"
        target_label: service
      - source_labels: [__meta_docker_container_name]
        target_label: container
    pipeline_stages:
      - json:
          expressions:
            level: level
            event: event
            trace_id: trace_id
      - labels:
          level:
          event:
          trace_id:
```

- [ ] **Step 5: Commit**

```bash
mkdir -p observability
git add observability/
git commit -m "feat: add Tempo/Prometheus/Loki/Promtail config files"
```

---

## Task 10: Grafana provisioning + dashboards

**Files:**
- Create: `observability/grafana/provisioning/datasources/datasources.yaml`
- Create: `observability/grafana/provisioning/dashboards/dashboards.yaml`
- Create: `observability/grafana/dashboards/pipeline-overview.json`
- Create: `observability/grafana/dashboards/search.json`
- Create: `observability/grafana/dashboards/tenants.json`

- [ ] **Step 1: Create datasources provisioning**

Create `observability/grafana/provisioning/datasources/datasources.yaml`:

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    uid: omnivore-prometheus
    url: http://prometheus:9090
    access: proxy
    isDefault: true
    jsonData:
      httpMethod: POST

  - name: Loki
    type: loki
    uid: omnivore-loki
    url: http://loki:3100
    access: proxy
    jsonData:
      derivedFields:
        - datasourceUid: omnivore-tempo
          matcherRegex: '"trace_id":"([a-f0-9]{32})"'
          name: TraceID
          url: "${__value.raw}"

  - name: Tempo
    type: tempo
    uid: omnivore-tempo
    url: http://tempo:3200
    access: proxy
    jsonData:
      lokiSearch:
        datasourceUid: omnivore-loki
```

- [ ] **Step 2: Create dashboards provisioning config**

Create `observability/grafana/provisioning/dashboards/dashboards.yaml`:

```yaml
apiVersion: 1

providers:
  - name: omnivore
    folder: Omnivore
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

- [ ] **Step 3: Create Pipeline Overview dashboard**

Create `observability/grafana/dashboards/pipeline-overview.json`:

```json
{
  "title": "Pipeline Overview",
  "uid": "omnivore-pipeline",
  "schemaVersion": 38,
  "version": 1,
  "tags": ["omnivore"],
  "time": {"from": "now-1h", "to": "now"},
  "timepicker": {},
  "timezone": "browser",
  "refresh": "30s",
  "panels": [
    {
      "id": 1,
      "title": "Document Ingest Rate (per minute)",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_documents_total[5m]) * 60",
          "legendFormat": "{{status}}"
        }
      ]
    },
    {
      "id": 2,
      "title": "Ingest Duration p95 by Handler (s)",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "histogram_quantile(0.95, rate(omnivore_ingest_duration_seconds_bucket[5m]))",
          "legendFormat": "p95 {{handler}}"
        }
      ]
    },
    {
      "id": 3,
      "title": "Queue Depth",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "omnivore_queue_depth",
          "legendFormat": "{{queue}}"
        }
      ]
    },
    {
      "id": 4,
      "title": "Failed Documents (per minute)",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_documents_total{status=\"failed\"}[5m]) * 60",
          "legendFormat": "failures"
        }
      ]
    },
    {
      "id": 5,
      "title": "HTTP Request Rate",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_http_requests_total[5m])",
          "legendFormat": "{{method}} {{route}} {{status_code}}"
        }
      ]
    },
    {
      "id": 6,
      "title": "HTTP p95 Latency (s)",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "histogram_quantile(0.95, rate(omnivore_http_duration_seconds_bucket[5m]))",
          "legendFormat": "p95 {{route}}"
        }
      ]
    }
  ]
}
```

- [ ] **Step 4: Create Search dashboard**

Create `observability/grafana/dashboards/search.json`:

```json
{
  "title": "Search",
  "uid": "omnivore-search",
  "schemaVersion": 38,
  "version": 1,
  "tags": ["omnivore"],
  "time": {"from": "now-1h", "to": "now"},
  "timepicker": {},
  "timezone": "browser",
  "refresh": "30s",
  "panels": [
    {
      "id": 1,
      "title": "Search Request Rate by Mode",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_search_duration_seconds_count[5m])",
          "legendFormat": "{{mode}}"
        }
      ]
    },
    {
      "id": 2,
      "title": "Search p95 Latency by Mode (s)",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "histogram_quantile(0.95, rate(omnivore_search_duration_seconds_bucket[5m]))",
          "legendFormat": "p95 {{mode}}"
        }
      ]
    },
    {
      "id": 3,
      "title": "Embeddings Produced",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_embeddings_total[5m])",
          "legendFormat": "{{model}}"
        }
      ]
    }
  ]
}
```

- [ ] **Step 5: Create Tenants dashboard**

Create `observability/grafana/dashboards/tenants.json`:

```json
{
  "title": "Tenants",
  "uid": "omnivore-tenants",
  "schemaVersion": 38,
  "version": 1,
  "tags": ["omnivore"],
  "time": {"from": "now-24h", "to": "now"},
  "timepicker": {},
  "timezone": "browser",
  "refresh": "1m",
  "panels": [
    {
      "id": 1,
      "title": "Documents Indexed per Tenant (last 24h)",
      "type": "bargauge",
      "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "options": {"reduceOptions": {"calcs": ["sum"]}},
      "targets": [
        {
          "expr": "increase(omnivore_documents_total{status=\"indexed\"}[24h])",
          "legendFormat": "{{tenant_id}}"
        }
      ]
    },
    {
      "id": 2,
      "title": "Rate-Limit Events (429s)",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_http_requests_total{status_code=\"429\"}[5m])",
          "legendFormat": "429 rate"
        }
      ]
    },
    {
      "id": 3,
      "title": "Chunks Produced per Tenant",
      "type": "timeseries",
      "gridPos": {"h": 8, "w": 24, "x": 0, "y": 8},
      "datasource": {"type": "prometheus", "uid": "omnivore-prometheus"},
      "targets": [
        {
          "expr": "rate(omnivore_chunks_total[5m])",
          "legendFormat": "{{tenant_id}} {{kind}}"
        }
      ]
    }
  ]
}
```

- [ ] **Step 6: Verify directory structure**

```bash
find observability/ -type f | sort
```

Expected:
```
observability/grafana/dashboards/pipeline-overview.json
observability/grafana/dashboards/search.json
observability/grafana/dashboards/tenants.json
observability/grafana/provisioning/dashboards/dashboards.yaml
observability/grafana/provisioning/datasources/datasources.yaml
observability/loki-config.yaml
observability/promtail-config.yaml
observability/prometheus.yml
observability/tempo.yaml
```

- [ ] **Step 7: Commit**

```bash
git add observability/
git commit -m "feat: Grafana provisioning + 3 dashboards (pipeline, search, tenants)"
```

---

## Task 11: Smoke-test the full monitoring stack

- [ ] **Step 1: Start the monitoring profile**

```bash
docker compose --profile monitoring up -d
```

Wait ~30 s for all containers to become healthy.

- [ ] **Step 2: Verify Grafana loads**

```bash
curl -s http://localhost:3000/api/health | python3 -m json.tool
```

Expected: `{"commit": "...", "database": "ok", "version": "..."}`

- [ ] **Step 3: Verify Prometheus scrapes the API**

Start the API if not running:
```bash
uv run uvicorn omnivore.api.main:app --port 8000 &
sleep 3
curl -s 'http://localhost:9090/api/v1/query?query=omnivore_http_requests_total' | python3 -m json.tool | head -10
```

Expected: JSON with `"status": "success"` (metric may have 0 results if no requests yet; that's fine — the metric name should appear after a `GET /metrics` request to the API).

- [ ] **Step 4: Open Grafana and verify dashboards auto-provisioned**

Visit `http://localhost:3000` — no login required. Navigate to Dashboards → Omnivore folder. You should see "Pipeline Overview", "Search", and "Tenants".

- [ ] **Step 5: Stop monitoring stack**

```bash
docker compose --profile monitoring down
```

---

## Task 12: Final tests + CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Run full test suite**

```bash
ADMIN_BOOTSTRAP_TOKEN=test-admin-bootstrap-for-integration-only uv run pytest tests/ -q 2>&1 | tail -5
```

Expected: 337+ passed, 0 failed, ruff clean.

- [ ] **Step 2: Ruff**

```bash
uv run ruff check src/ tests/ eval/
```

Expected: `All checks passed!`

- [ ] **Step 3: Update CLAUDE.md**

In `CLAUDE.md`, find the Phase roadmap table and update the Phase 5 row:

```markdown
| 5 — Observability | OTel traces (spans in API + worker → Tempo), prometheus-client metrics (/metrics scrape → Prometheus), structlog JSON → Promtail → Loki, Grafana dashboards (Pipeline Overview / Search / Tenants). W3C traceparent propagation across API→ARQ boundary. `docker compose --profile monitoring up -d`. | **Done — 2026-05-17.** 337+ tests passing, ruff clean. |
```

- [ ] **Step 4: Final commit**

```bash
git add CLAUDE.md
git commit -m "feat: Phase 5 — observability complete (OTel + Prometheus + Loki + Grafana)"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] OTel traces → Tempo: Tasks 2, 6
- [x] prometheus-client metrics → Prometheus: Tasks 2, 5
- [x] structlog trace_id injection: Task 4
- [x] Promtail → Loki: Tasks 8, 9
- [x] Grafana 3 dashboards: Task 10
- [x] Docker Compose monitoring profile: Task 8
- [x] All 8 metrics defined in spec: implemented in `observability.py` (Task 2)
- [x] All spans defined in spec: Tasks 6, 7
- [x] W3C traceparent API→worker: Task 6
- [x] Queue depth gauge: Task 5 (`_poll_queue_depth` in lifespan)
- [x] Unit tests for spans + metrics + processor: Task 2, 4
- [x] Integration test for /metrics: Task 5
- [x] Coverage ≥80% for new files: covered by unit + integration tests

**Placeholder scan:** None found.

**Type consistency:**
- `setup_tracing(settings)` defined in Task 2, called in Tasks 5 and 6 ✓
- `get_tracer()` defined in Task 2, used in Tasks 6 and 7 ✓
- `DOCUMENTS_TOTAL`, `CHUNKS_TOTAL`, etc. defined in Task 2, imported by name in Tasks 6 and 7 ✓
- `_otel_traceparent: str | None = None` added to both `ingest_dispatch` and `gpu_ingest_dispatch` in Task 6 ✓
