"""Chaos tests: backpressure and outbox relay resilience.

Tests queue-depth rejection (429) and outbox_relay behaviour under Redis
failure.  All calls are unit-test style — no real infrastructure required.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from omnivore.api.routes.documents import upload_document
from omnivore.worker.tasks import outbox_relay

from .conftest import (
    make_arq_pool,
    make_db,
    make_request,
    make_upload,
)

# ---------------------------------------------------------------------------
# Queue depth → 429 (CPU queue)
# ---------------------------------------------------------------------------

async def test_upload_returns_429_when_cpu_queue_depth_exceeded(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """Upload rejected with 429 when queue_depth >= MAX_QUEUE_DEPTH (100)."""
    db = make_db(scalar_return=None)
    # queue_depth == MAX_QUEUE_DEPTH triggers the guard (>=)
    arq_pool = make_arq_pool(queue_depth=100)
    req = make_request(arq_pool=arq_pool)
    upload = make_upload(b"backpressure test content")
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_document(request=req, file=upload, db=db, auth=auth_ctx)

    assert exc_info.value.status_code == 429
    assert "Queue full" in exc_info.value.detail


async def test_upload_returns_429_one_above_cpu_queue_limit(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """queue_depth strictly > MAX_QUEUE_DEPTH also triggers 429."""
    db = make_db(scalar_return=None)
    arq_pool = make_arq_pool(queue_depth=101)
    req = make_request(arq_pool=arq_pool)
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_document(
                request=req,
                file=make_upload(b"over limit"),
                db=db,
                auth=auth_ctx,
            )

    assert exc_info.value.status_code == 429


async def test_upload_accepted_one_below_cpu_queue_limit(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """queue_depth == MAX_QUEUE_DEPTH - 1 must be accepted (boundary condition)."""
    db = make_db(scalar_return=None)
    arq_pool = make_arq_pool(queue_depth=99)  # one below the 100 limit
    req = make_request(arq_pool=arq_pool)
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(
            request=req,
            file=make_upload(b"just under limit"),
            db=db,
            auth=auth_ctx,
        )

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Queue depth → 429 (GPU queue)
# ---------------------------------------------------------------------------

async def test_upload_returns_429_when_gpu_queue_depth_exceeded(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """GPU file upload rejected with 429 when gpu queue depth >= MAX_GPU_QUEUE_DEPTH (20)."""
    db = make_db(scalar_return=None)
    arq_pool = AsyncMock()
    arq_pool.default_queue_name = "arq:queue"
    arq_pool.zcard = AsyncMock(
        side_effect=lambda key: {
            "arq:queue": 0,   # CPU queue fine
            "arq:gpu": 20,    # GPU queue at limit
        }.get(key, 0)
    )
    req = make_request(arq_pool=arq_pool)
    mock_session, _ = mock_s3_session

    gpu_handler = MagicMock()
    gpu_handler.cost_class = "gpu"

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="audio/mpeg"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
        patch("omnivore.api.routes.documents.registry.resolve", return_value=gpu_handler),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_document(
                request=req,
                file=make_upload(b"fake audio", filename="audio.mp3"),
                db=db,
                auth=auth_ctx,
            )

    assert exc_info.value.status_code == 429
    assert "GPU queue full" in exc_info.value.detail


async def test_cpu_upload_not_rejected_when_only_gpu_queue_full(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """Non-GPU uploads are not checked against the GPU queue."""
    db = make_db(scalar_return=None)
    arq_pool = AsyncMock()
    arq_pool.default_queue_name = "arq:queue"
    arq_pool.zcard = AsyncMock(
        side_effect=lambda key: {
            "arq:queue": 0,   # CPU queue fine
            "arq:gpu": 999,   # GPU queue saturated — irrelevant for CPU files
        }.get(key, 0)
    )
    req = make_request(arq_pool=arq_pool)
    mock_session, _ = mock_s3_session

    cpu_handler = MagicMock()
    cpu_handler.cost_class = "cpu"

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
        patch("omnivore.api.routes.documents.registry.resolve", return_value=cpu_handler),
    ):
        resp = await upload_document(
            request=req,
            file=make_upload(b"plain text content"),
            db=db,
            auth=auth_ctx,
        )

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Outbox relay resilience under Redis failure
# ---------------------------------------------------------------------------

def _make_outbox_session(rows: list):
    """Return a session factory mock that yields the given outbox rows."""
    db = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=rows)
    db.scalars = AsyncMock(return_value=scalars_result)
    db.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=cm)
    return session_factory, db


def _make_outbox_row(row_id: int = 1) -> MagicMock:
    row = MagicMock()
    row.id = row_id
    row.payload = {
        "task": "ingest_dispatch",
        "kwargs": {"document_id": str(uuid.uuid4())},
    }
    row.published_at = None
    return row


async def test_outbox_relay_handles_redis_failure_gracefully():
    """enqueue_job raises ConnectionError — relay catches it, returns published=0."""
    row = _make_outbox_row()
    session_factory, db = _make_outbox_session([row])

    redis = AsyncMock()
    redis.enqueue_job = AsyncMock(side_effect=ConnectionError("Redis unavailable"))

    with patch("omnivore.worker.tasks.admin_session", session_factory):
        result = await outbox_relay({"redis": redis})

    assert result == {"published": 0}
    # The row's published_at must remain None (not marked published)
    assert row.published_at is None


async def test_outbox_relay_does_not_crash_on_redis_failure():
    """Relay must not propagate Redis exceptions — it must return a result dict."""
    row = _make_outbox_row()
    session_factory, _ = _make_outbox_session([row])

    redis = AsyncMock()
    redis.enqueue_job = AsyncMock(side_effect=OSError("connection refused"))

    with patch("omnivore.worker.tasks.admin_session", session_factory):
        result = await outbox_relay({"redis": redis})

    # Must return a dict (not raise)
    assert isinstance(result, dict)
    assert "published" in result


async def test_outbox_relay_publishes_as_many_as_possible_on_partial_redis_failure():
    """First row succeeds, second raises — first is marked published, second is not."""
    row_ok = _make_outbox_row(row_id=1)
    row_fail = _make_outbox_row(row_id=2)
    session_factory, db = _make_outbox_session([row_ok, row_fail])

    call_count = 0

    async def _partial_enqueue(task, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ConnectionError("redis transient failure")

    redis = AsyncMock()
    redis.enqueue_job = AsyncMock(side_effect=_partial_enqueue)

    with patch("omnivore.worker.tasks.admin_session", session_factory):
        result = await outbox_relay({"redis": redis})

    assert result == {"published": 1}
    assert row_ok.published_at is not None
    assert row_fail.published_at is None


async def test_outbox_relay_empty_queue_returns_zero():
    """No unpublished rows → published=0 without touching Redis."""
    session_factory, _ = _make_outbox_session([])
    redis = AsyncMock()

    with patch("omnivore.worker.tasks.admin_session", session_factory):
        result = await outbox_relay({"redis": redis})

    assert result == {"published": 0}
    redis.enqueue_job.assert_not_called()


async def test_outbox_relay_commits_after_publish():
    """db.commit must be called after rows are published (durability guarantee)."""
    row = _make_outbox_row()
    session_factory, db = _make_outbox_session([row])

    redis = AsyncMock()
    redis.enqueue_job = AsyncMock()

    with patch("omnivore.worker.tasks.admin_session", session_factory):
        await outbox_relay({"redis": redis})

    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# Rate-limit 429 from check_rate_limit (token bucket exhausted)
# ---------------------------------------------------------------------------

async def test_upload_returns_429_when_rate_limit_exhausted(
    auth_ctx,
    mock_settings,
    mock_s3_session,
):
    """Token bucket exhausted → route returns 429 with Retry-After header."""
    rl_denied = MagicMock()
    rl_denied.allowed = False
    rl_denied.remaining = 0.0
    rl_denied.capacity = 100
    rl_denied.reset_after_seconds = 5

    db = make_db(scalar_return=None)
    req = make_request(arq_pool=make_arq_pool())
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_denied)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(
            request=req,
            file=make_upload(b"rate limited content"),
            db=db,
            auth=auth_ctx,
        )

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 429
    assert body["success"] is False
    assert body["error"]["code"] == "RATE_LIMITED"
    # No document row committed when rate-limited
    db.commit.assert_not_awaited()
