"""Chaos tests: infrastructure failures at upload time.

Calls upload_document() directly (unit-test style) to avoid
real network I/O. Mirrors tests/unit/test_routes_documents.py.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnivore.api.routes.documents import upload_document

from .conftest import (
    _TEST_TENANT_ID,
    make_arq_pool,
    make_db,
    make_request,
    make_upload,
)

# ---------------------------------------------------------------------------
# test_upload_returns_202_when_redis_enqueue_fails
#
# The documents route wraps pool.enqueue_job in try/except and logs a warning;
# the outbox_relay safety net will pick up the unpublished entry.  The caller
# therefore still receives 202 / "queued".  This is the ACTUAL behaviour —
# do not test an idealised 503 that the code does not produce.
# ---------------------------------------------------------------------------

async def test_upload_returns_202_when_redis_enqueue_fails(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """ARQ enqueue raises ConnectionError — route catches it and returns 202 queued.

    The document row and the unpublished outbox entry were already committed;
    the outbox_relay cron will re-enqueue when Redis recovers.
    """
    db = make_db(scalar_return=None)
    arq_pool = make_arq_pool(queue_depth=0)
    arq_pool.enqueue_job = AsyncMock(side_effect=ConnectionError("Redis unavailable"))
    req = make_request(arq_pool=arq_pool)
    upload = make_upload(b"upload chaos content")
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=auth_ctx)

    body = json.loads(resp.body)
    # The route catches the exception — outbox entry remains for relay
    assert resp.status_code == 202
    assert body["success"] is True
    assert body["data"]["status"] == "queued"
    # enqueue_job was attempted once and failed
    arq_pool.enqueue_job.assert_awaited_once()
    # Document row was still committed to DB
    db.commit.assert_awaited()


async def test_upload_document_row_committed_before_redis_enqueue(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """Verify that db.commit is called BEFORE pool.enqueue_job — the outbox is durable."""
    commit_order: list[str] = []

    db = make_db(scalar_return=None)

    async def _tracking_commit():
        commit_order.append("db.commit")

    db.commit = AsyncMock(side_effect=_tracking_commit)

    arq_pool = make_arq_pool(queue_depth=0)

    async def _tracking_enqueue(*args, **kwargs):
        commit_order.append("enqueue_job")

    arq_pool.enqueue_job = AsyncMock(side_effect=_tracking_enqueue)
    req = make_request(arq_pool=arq_pool)
    upload = make_upload(b"order test content")
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        await upload_document(request=req, file=upload, db=db, auth=auth_ctx)

    # The first commit must precede the enqueue_job call
    assert "db.commit" in commit_order
    assert "enqueue_job" in commit_order
    first_commit_idx = commit_order.index("db.commit")
    enqueue_idx = commit_order.index("enqueue_job")
    assert first_commit_idx < enqueue_idx, (
        f"db.commit must happen before enqueue_job; got order={commit_order}"
    )


# ---------------------------------------------------------------------------
# test_upload_returns_500_when_minio_upload_fails
#
# aioboto3 upload_fileobj raises — the route has no explicit catch for S3
# errors in the upload path, so the global FastAPI exception handler takes
# over and returns 500.  We verify the exception propagates rather than being
# silently swallowed.
# ---------------------------------------------------------------------------

async def test_upload_raises_when_minio_upload_fails(
    auth_ctx,
    rl_allowed,
    mock_settings,
):
    """S3 upload_fileobj raises — unhandled, propagates as-is.

    The route does not catch S3 errors in the upload path.  Any exception from
    upload_fileobj will propagate to FastAPI's global exception handler (500).
    We verify the exception is NOT silently swallowed.
    """
    db = make_db(scalar_return=None)
    req = make_request(arq_pool=make_arq_pool())
    upload = make_upload(b"s3 chaos content")

    s3 = AsyncMock()
    s3.head_bucket = AsyncMock()
    s3.upload_fileobj = AsyncMock(side_effect=ConnectionError("MinIO unavailable"))
    s3_cm = AsyncMock()
    s3_cm.__aenter__ = AsyncMock(return_value=s3)
    s3_cm.__aexit__ = AsyncMock(return_value=False)
    failing_session = MagicMock()
    failing_session.client = MagicMock(return_value=s3_cm)

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=failing_session),
    ):
        with pytest.raises(ConnectionError, match="MinIO unavailable"):
            await upload_document(request=req, file=upload, db=db, auth=auth_ctx)

    # S3 was called but failed
    s3.upload_fileobj.assert_awaited_once()
    # DB was NOT committed (exception thrown before commit)
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_upload_when_duplicate_sha256_returns_202_with_duplicate_status
#
# Regression test: uploading the same file twice returns the existing document
# with status="duplicate" — no new document row, no S3 upload, no ARQ enqueue.
# ---------------------------------------------------------------------------

async def test_upload_duplicate_sha256_returns_duplicate_status(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """Second upload of the same content returns status='duplicate' with the existing doc ID."""
    existing_id = uuid.uuid4()
    existing_doc = MagicMock()
    existing_doc.id = existing_id
    existing_doc.tenant_id = _TEST_TENANT_ID

    # scalar returns the existing document (dedup match)
    db = make_db(scalar_return=existing_doc)
    req = make_request(arq_pool=make_arq_pool())
    upload = make_upload(b"duplicate document content")
    mock_session, mock_s3 = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=auth_ctx)

    body = json.loads(resp.body)
    assert resp.status_code == 202
    assert body["success"] is True
    assert body["data"]["status"] == "duplicate"
    assert body["data"]["document_id"] == str(existing_id)


async def test_upload_duplicate_does_not_call_s3(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """Duplicate detection short-circuits before S3 — no upload attempt."""
    existing_doc = MagicMock()
    existing_doc.id = uuid.uuid4()
    existing_doc.tenant_id = _TEST_TENANT_ID

    db = make_db(scalar_return=existing_doc)
    mock_session, mock_s3 = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session") as session_cls,
    ):
        await upload_document(
            request=make_request(),
            file=make_upload(b"dup"),
            db=db,
            auth=auth_ctx,
        )

    # aioboto3.Session was never instantiated (short-circuit before S3)
    session_cls.assert_not_called()


async def test_upload_duplicate_does_not_enqueue_arq_job(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """Duplicate detection short-circuits before ARQ enqueue."""
    existing_doc = MagicMock()
    existing_doc.id = uuid.uuid4()
    existing_doc.tenant_id = _TEST_TENANT_ID

    db = make_db(scalar_return=existing_doc)
    arq_pool = make_arq_pool()
    req = make_request(arq_pool=arq_pool)
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        await upload_document(request=req, file=make_upload(b"dup"), db=db, auth=auth_ctx)

    arq_pool.enqueue_job.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_upload_without_arq_pool_still_returns_queued
#
# No ARQ pool on app.state — the route writes the document + outbox row and
# returns 202 queued.  The outbox_relay cron will enqueue when a pool exists.
# ---------------------------------------------------------------------------

async def test_upload_without_arq_pool_still_returns_queued(
    auth_ctx,
    rl_allowed,
    mock_settings,
    mock_s3_session,
):
    """No ARQ pool on app.state: document persisted via outbox, returns 202 queued."""
    db = make_db(scalar_return=None)
    req = make_request(arq_pool=None)  # no pool
    upload = make_upload(b"no pool content")
    mock_session, _ = mock_s3_session

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=rl_allowed)),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=mock_settings),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=auth_ctx)

    body = json.loads(resp.body)
    assert resp.status_code == 202
    assert body["data"]["status"] == "queued"
    db.commit.assert_awaited()
