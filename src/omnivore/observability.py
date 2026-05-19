from __future__ import annotations

import prometheus_client
import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from omnivore.config import Settings

logger = structlog.get_logger(__name__)

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

STORAGE_BYTES_TOTAL = prometheus_client.Counter(
    "omnivore_storage_bytes_total",
    "Cumulative bytes of documents ingested",
    ["tenant_id", "mime_type"],
)

# ---------------------------------------------------------------------------
# M-1: Cardinality cap for tenant_id labels — prevents unbounded label explosion.
# When more than _TENANT_LABEL_CAP distinct tenant IDs have been seen, excess
# IDs are normalised to "__other__" so Prometheus cardinality stays bounded.
# ---------------------------------------------------------------------------

_TENANT_LABEL_CAP: int = 200
_seen_tenant_ids: set[str] = set()


def _cap_tenant_id(tenant_id: str) -> str:
    """Return tenant_id as-is until the cap is reached; then return '__other__'."""
    if tenant_id in _seen_tenant_ids:
        return tenant_id
    if len(_seen_tenant_ids) < _TENANT_LABEL_CAP:
        _seen_tenant_ids.add(tenant_id)
        return tenant_id
    return "__other__"


# ---------------------------------------------------------------------------
# Setup functions — called once at process startup
# ---------------------------------------------------------------------------


def setup_metrics(settings: Settings) -> None:  # noqa: ARG001
    """No-op: metrics are initialised at import time as module-level singletons."""


def setup_tracing(settings: Settings) -> None:
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
        "observability.tracing.configured",
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        service=settings.OTEL_SERVICE_NAME,
    )


def get_tracer(name: str = "omnivore") -> trace.Tracer:
    """Return the global tracer. Always safe to call — falls back to NoOp."""
    return trace.get_tracer(name)
