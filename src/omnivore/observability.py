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
