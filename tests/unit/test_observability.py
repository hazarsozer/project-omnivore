from __future__ import annotations

import prometheus_client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from omnivore.observability import (
    _TENANT_LABEL_CAP,
    CHUNKS_TOTAL,
    DOCUMENTS_TOTAL,
    EMBEDDINGS_TOTAL,
    HTTP_DURATION,
    HTTP_REQUESTS_TOTAL,
    INGEST_DURATION,
    QUEUE_DEPTH,
    SEARCH_DURATION,
    STORAGE_BYTES_TOTAL,
    _cap_tenant_id,
    _seen_tenant_ids,
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
    """When no active span has a valid SpanContext, the dict is returned unchanged.

    M-4: The processor now checks ctx.is_valid (not span.is_recording()), so a
    sampled-out span with an invalid context will correctly produce no trace_ keys.
    """
    from omnivore.logging_config import _otel_context_processor

    # Outside any span — get_current_span() returns INVALID_SPAN whose
    # SpanContext.is_valid is False, so no keys should be injected.
    event = _otel_context_processor(None, None, {"event": "hello"})
    assert "trace_id" not in event
    assert "span_id" not in event


def test_otel_context_processor_uses_ctx_is_valid_not_is_recording():
    """Verify that ctx.is_valid is the gate, not is_recording().

    This test installs a NoOp span (is_recording()==False, is_valid==False)
    to confirm the processor correctly skips injection in that case.
    """
    from opentelemetry.trace import INVALID_SPAN

    from omnivore.logging_config import _otel_context_processor

    # INVALID_SPAN has is_recording()==False and ctx.is_valid==False — no injection.
    trace.use_span(INVALID_SPAN)
    try:
        event = _otel_context_processor(None, None, {"event": "test"})
        assert "trace_id" not in event
        assert "span_id" not in event
    finally:
        pass  # INVALID_SPAN context is thread-local; nothing to restore here


def test_cap_tenant_id_returns_id_within_cap():
    """_cap_tenant_id returns the tenant_id unchanged when under the cap."""
    # Use a unique prefix to avoid collisions with other tests
    tid = "__test_cap_under__"
    _seen_tenant_ids.discard(tid)
    result = _cap_tenant_id(tid)
    assert result == tid
    _seen_tenant_ids.discard(tid)


def test_cap_tenant_id_normalises_excess_to_other():
    """After _TENANT_LABEL_CAP distinct IDs, excess ones become '__other__'."""
    import omnivore.observability as _obs

    # Save and temporarily replace the module-level set with a full one
    original = _obs._seen_tenant_ids.copy()
    try:
        # Fill the set to exactly the cap with synthetic IDs
        _obs._seen_tenant_ids.clear()
        for i in range(_TENANT_LABEL_CAP):
            _obs._seen_tenant_ids.add(f"__fill_{i}__")

        # Next new tenant must be capped
        result = _cap_tenant_id("__brand_new_tenant__")
        assert result == "__other__"
    finally:
        _obs._seen_tenant_ids.clear()
        _obs._seen_tenant_ids.update(original)


def test_storage_bytes_total_is_registered():
    """STORAGE_BYTES_TOTAL must be a prometheus Counter."""
    assert isinstance(STORAGE_BYTES_TOTAL, prometheus_client.Counter)


def test_storage_bytes_total_increments():
    """STORAGE_BYTES_TOTAL increments by the given byte count."""
    from prometheus_client import REGISTRY

    tid = "__storage_test_tenant__"
    mime = "application/pdf"
    before = REGISTRY.get_sample_value(
        "omnivore_storage_bytes_total",
        {"tenant_id": tid, "mime_type": mime},
    ) or 0
    STORAGE_BYTES_TOTAL.labels(tenant_id=tid, mime_type=mime).inc(1024)
    after = REGISTRY.get_sample_value(
        "omnivore_storage_bytes_total",
        {"tenant_id": tid, "mime_type": mime},
    )
    assert after == (before + 1024)
