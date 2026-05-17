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
