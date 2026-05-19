"""Integration: /metrics endpoint returns Prometheus exposition format.

Uses ASGITransport — no real Prometheus needed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from omnivore.api.main import app
from omnivore.config import get_settings
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


def test_documents_total_metric_increments_on_success():
    """Worker-side metric is reachable in-process (regression guard for C-1 fix).

    Verifies that DOCUMENTS_TOTAL is correctly registered in the global Prometheus
    registry and that calling .labels(...).inc() produces a readable sample value.
    This is the in-process analogue of scraping worker:9101/metrics.
    """
    from prometheus_client import REGISTRY

    from omnivore.observability import DOCUMENTS_TOTAL

    before = REGISTRY.get_sample_value(
        "omnivore_documents_total",
        {"status": "indexed", "tenant_id": "c1-regression", "mime_type": "text/plain"},
    ) or 0
    DOCUMENTS_TOTAL.labels(
        status="indexed", tenant_id="c1-regression", mime_type="text/plain"
    ).inc()
    after = REGISTRY.get_sample_value(
        "omnivore_documents_total",
        {"status": "indexed", "tenant_id": "c1-regression", "mime_type": "text/plain"},
    )
    assert after == (before + 1)


# ---------------------------------------------------------------------------
# H-3: Route cardinality — unmatched paths must not pollute metric labels
# ---------------------------------------------------------------------------


def test_unmatched_route_label():
    """Requests to unknown paths are labelled route='__unmatched__' in /metrics."""
    results: dict = {}

    async def _run():
        registry.discover()
        app.state.arq_pool = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Hit a path that doesn't exist — triggers __unmatched__ label
            await c.get("/nonexistent-path-xyz")
            r = await c.get("/metrics")
            results["body"] = r.text

    asyncio.run(_run())
    assert 'route="__unmatched__"' in results["body"]


# ---------------------------------------------------------------------------
# H-4: /metrics bearer token auth
# ---------------------------------------------------------------------------


@pytest.fixture()
def metrics_auth_results():
    """Run /metrics requests with METRICS_AUTH_TOKEN set to 'test-secret-token'."""
    results: dict = {}

    async def _run():
        registry.discover()
        app.state.arq_pool = None

        get_settings.cache_clear()
        with patch.dict(
            "os.environ",
            {"METRICS_AUTH_TOKEN": "test-secret-token"},
            clear=False,
        ):
            get_settings.cache_clear()
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as c:
                    # No auth header → 403
                    r_no_auth = await c.get("/metrics")
                    results["no_auth_status"] = r_no_auth.status_code

                    # Wrong token → 403
                    r_wrong = await c.get(
                        "/metrics", headers={"Authorization": "Bearer wrong-token"}
                    )
                    results["wrong_token_status"] = r_wrong.status_code

                    # Correct token → 200
                    r_ok = await c.get(
                        "/metrics",
                        headers={"Authorization": "Bearer test-secret-token"},
                    )
                    results["ok_status"] = r_ok.status_code
                    results["ok_body"] = r_ok.text
            finally:
                get_settings.cache_clear()

    asyncio.run(_run())
    return results


def test_metrics_no_auth_returns_401(metrics_auth_results):
    assert metrics_auth_results["no_auth_status"] == 401


def test_metrics_wrong_token_returns_401(metrics_auth_results):
    assert metrics_auth_results["wrong_token_status"] == 401


def test_metrics_correct_token_returns_200(metrics_auth_results):
    assert metrics_auth_results["ok_status"] == 200


def test_metrics_correct_token_body_is_prometheus(metrics_auth_results):
    assert "omnivore_http_requests_total" in metrics_auth_results["ok_body"]
