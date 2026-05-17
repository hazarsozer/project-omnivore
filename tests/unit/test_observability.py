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
    provider.shutdown()  # stop background exporter thread to avoid noisy retry output


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
    from omnivore.logging_config import _otel_context_processor

    event = _otel_context_processor(None, None, {"event": "hello"})
    assert "trace_id" not in event
    assert "span_id" not in event
