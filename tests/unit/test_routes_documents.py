"""Unit tests for api/routes/documents.py — upload, dedup, get, list, delete."""
from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from omnivore.api.routes.documents import (
    delete_document,
    get_document,
    get_document_entities,
    list_documents,
    retry_document,
    upload_document,
)
from omnivore.auth.context import AuthContext

_TEST_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALL_SCOPES = frozenset([
    "documents:read", "documents:write",
    "chunks:read", "entities:read",
    "search:read", "handlers:read", "tenant:manage",
])


def _make_auth() -> AuthContext:
    return AuthContext(
        tenant_id=_TEST_TENANT_ID,
        principal_id="test-key-id",
        principal_type="api_key",
        scopes=_ALL_SCOPES,
        raw_token_hash="",
    )


def _rl_allowed():
    """Return a mock RateLimitResult that allows the request."""
    rl = MagicMock()
    rl.allowed = True
    rl.remaining = 90.0
    rl.capacity = 100
    rl.reset_after_seconds = 0
    return rl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc(doc_id: uuid.UUID | None = None) -> MagicMock:
    doc = MagicMock()
    doc.id = doc_id or uuid.uuid4()
    doc.tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    doc.filename = "test.txt"
    doc.mime_type = "text/plain"
    doc.size_bytes = 100
    doc.status = "indexed"
    doc.handler_name = "text"
    doc.handler_version = "1.0.0"
    doc.storage_uri = "s3://bucket/raw/test.txt"
    doc.sha256 = b"\x00" * 32
    doc.doc_metadata = {}
    doc.error = None
    doc.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    doc.indexed_at = datetime(2026, 1, 2, tzinfo=UTC)
    return doc


def _make_db(scalar_return=None, scalars_return=None, get_return=None) -> AsyncMock:
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=scalar_return)
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=scalars_return or [])
    db.scalars = AsyncMock(return_value=scalars_mock)
    db.get = AsyncMock(return_value=get_return)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _make_upload_mock(content: bytes = b"hello file", filename: str = "test.txt") -> MagicMock:
    f = MagicMock()
    f.filename = filename
    f.read = AsyncMock(side_effect=[content, b""])
    f.seek = AsyncMock()
    f.file = io.BytesIO(content)
    return f


def _make_request(arq_pool=None, queue_depth: int = 0) -> MagicMock:
    req = MagicMock()
    req.app = MagicMock()
    req.app.state = MagicMock()
    if arq_pool is not None:
        arq_pool.zcard = AsyncMock(return_value=queue_depth)
        arq_pool.default_queue_name = "arq:queue"
    req.app.state.arq_pool = arq_pool
    return req


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
    s.MAX_QUEUE_DEPTH = 100
    s.MAX_GPU_QUEUE_DEPTH = 20
    s.MINIO_ENDPOINT = "localhost:9000"
    s.MINIO_SECURE = False
    s.MINIO_ACCESS_KEY = "minioadmin"
    s.MINIO_SECRET_KEY = MagicMock()
    s.MINIO_SECRET_KEY.get_secret_value.return_value = "minioadmin"
    s.MINIO_BUCKET = "omnivore"
    s.RL_UPLOAD_COST = 10
    return s


def _mock_s3_session() -> MagicMock:
    s3 = AsyncMock()
    s3.head_bucket = AsyncMock()
    s3.upload_fileobj = AsyncMock()

    s3_cm = AsyncMock()
    s3_cm.__aenter__ = AsyncMock(return_value=s3)
    s3_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.client = MagicMock(return_value=s3_cm)
    return session, s3


# ---------------------------------------------------------------------------
# GET /documents/{document_id}
# ---------------------------------------------------------------------------

async def test_get_document_found():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    db = _make_db(get_return=doc)

    resp = await get_document(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.success is True
    assert resp.data["document_id"] == str(doc_id)
    assert resp.data["filename"] == "test.txt"
    assert resp.data["status"] == "indexed"
    assert resp.data["created_at"] == "2026-01-01T00:00:00+00:00"


async def test_get_document_includes_indexed_at():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    db = _make_db(get_return=doc)

    resp = await get_document(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.data["indexed_at"] is not None


async def test_get_document_not_found_raises_404():
    db = _make_db(get_return=None)

    with pytest.raises(HTTPException) as exc_info:
        await get_document(document_id=uuid.uuid4(), db=db, auth=_make_auth())

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /documents (list)
# ---------------------------------------------------------------------------

async def test_list_documents_returns_all():
    docs = [_make_doc() for _ in range(3)]
    db = _make_db(scalars_return=docs)

    resp = await list_documents(db=db, auth=_make_auth(), status=None, limit=50, cursor=None)

    assert resp.success is True
    assert len(resp.data) == 3
    assert resp.meta["count"] == 3


async def test_list_documents_empty():
    db = _make_db(scalars_return=[])

    resp = await list_documents(db=db, auth=_make_auth(), status=None, limit=50, cursor=None)

    assert resp.success is True
    assert resp.data == []
    assert resp.meta["count"] == 0


async def test_list_documents_response_shape():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    db = _make_db(scalars_return=[doc])

    resp = await list_documents(db=db, auth=_make_auth(), status=None, limit=50, cursor=None)

    item = resp.data[0]
    assert item["document_id"] == str(doc_id)
    assert item["filename"] == "test.txt"
    assert item["mime_type"] == "text/plain"
    assert item["status"] == "indexed"
    assert "created_at" in item


# ---------------------------------------------------------------------------
# POST /documents — deduplication
# ---------------------------------------------------------------------------

async def test_upload_document_dedup_returns_existing():
    existing = _make_doc()
    db = _make_db(scalar_return=existing)
    req = _make_request()
    upload = _make_upload_mock(b"duplicate content")

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session"),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 202
    assert body["success"] is True
    assert body["data"]["status"] == "duplicate"
    assert body["data"]["document_id"] == str(existing.id)


async def test_upload_document_dedup_does_not_call_s3():
    existing = _make_doc()
    db = _make_db(scalar_return=existing)
    upload = _make_upload_mock(b"dup")

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session") as mock_session_cls,
    ):
        await upload_document(request=_make_request(), file=upload, db=db, auth=_make_auth())

    # Session was never instantiated (short-circuit before S3)
    mock_session_cls.assert_not_called()


# ---------------------------------------------------------------------------
# POST /documents — new upload
# ---------------------------------------------------------------------------

async def test_upload_document_new_returns_queued():
    db = _make_db(scalar_return=None)
    arq_pool = AsyncMock()
    req = _make_request(arq_pool=arq_pool)
    upload = _make_upload_mock(b"brand new document content")
    mock_session, _ = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    import json
    body = json.loads(resp.body)
    assert resp.status_code == 202
    assert body["success"] is True
    assert body["data"]["status"] == "queued"
    assert "document_id" in body["data"]
    assert "poll_url" in body["data"]


async def test_upload_document_new_enqueues_arq_job():
    db = _make_db(scalar_return=None)
    arq_pool = AsyncMock()
    req = _make_request(arq_pool=arq_pool)
    upload = _make_upload_mock(b"content to ingest")
    mock_session, _ = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    arq_pool.enqueue_job.assert_awaited_once()
    call_kwargs = arq_pool.enqueue_job.call_args
    assert call_kwargs[0][0] == "ingest_dispatch"


async def test_upload_document_no_arq_pool_still_queued():
    """No ARQ pool: document is still written to DB + outbox; enqueue is skipped."""
    db = _make_db(scalar_return=None)
    req = _make_request(arq_pool=None)
    upload = _make_upload_mock(b"content")
    mock_session, _ = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    import json
    body = json.loads(resp.body)
    assert body["data"]["status"] == "queued"


async def test_upload_document_uploads_to_s3():
    db = _make_db(scalar_return=None)
    req = _make_request(arq_pool=AsyncMock())
    upload = _make_upload_mock(b"data")
    mock_session, mock_s3 = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    mock_s3.upload_fileobj.assert_awaited_once()
    call_args = mock_s3.upload_fileobj.call_args
    assert call_args[0][1] == "omnivore"  # bucket name


async def test_upload_document_413_on_oversized_file():
    settings = _mock_settings()
    settings.MAX_UPLOAD_SIZE_BYTES = 5  # tiny limit

    db = _make_db(scalar_return=None)
    upload = _make_upload_mock(b"0123456789")  # 10 bytes > 5

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=settings),
        patch("omnivore.api.routes.documents.aioboto3.Session"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_document(request=_make_request(), file=upload, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 413


async def test_upload_document_ensure_bucket_creates_if_missing():
    """_ensure_bucket creates the bucket when head_bucket raises."""
    db = _make_db(scalar_return=None)
    req = _make_request(arq_pool=AsyncMock())
    upload = _make_upload_mock(b"content")

    s3 = AsyncMock()
    s3.head_bucket = AsyncMock(side_effect=Exception("NoSuchBucket"))
    s3.create_bucket = AsyncMock()
    s3.upload_fileobj = AsyncMock()

    s3_cm = AsyncMock()
    s3_cm.__aenter__ = AsyncMock(return_value=s3)
    s3_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.client = MagicMock(return_value=s3_cm)

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    s3.create_bucket.assert_awaited_once_with(Bucket="omnivore")


async def test_list_documents_with_status_filter():
    docs = [_make_doc()]
    db = _make_db(scalars_return=docs)

    resp = await list_documents(db=db, auth=_make_auth(), status="indexed", limit=50, cursor=None)

    assert resp.success is True
    assert len(resp.data) == 1


async def test_list_documents_invalid_status_ignored():
    """An unrecognised status string is silently ignored (not added to WHERE clause)."""
    docs = [_make_doc()]
    db = _make_db(scalars_return=docs)

    resp = await list_documents(db=db, auth=_make_auth(), status="bogus_status", limit=50, cursor=None)

    assert resp.success is True


async def test_list_documents_with_cursor():
    docs = [_make_doc()]
    db = _make_db(scalars_return=docs)

    resp = await list_documents(db=db, auth=_make_auth(), status=None, limit=50, cursor="2026-01-01T00:00:00+00:00")

    assert resp.success is True


async def test_list_documents_invalid_cursor_returns_422():
    """A malformed cursor must surface as 422 in the APIResponse envelope, not
    silently fall through to an unrelated first page."""
    db = _make_db(scalars_return=[_make_doc()])

    resp = await list_documents(db=db, auth=_make_auth(), status=None, limit=50, cursor="not-a-datetime")

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_CURSOR"
    # Must not have executed the query with a bogus cursor.
    db.scalars.assert_not_called()


# ---------------------------------------------------------------------------
# POST /documents — ARQ enqueue failure (outbox relay safety net)
# ---------------------------------------------------------------------------

async def test_upload_document_arq_enqueue_failure_does_not_raise():
    """S3 succeeded + DB committed, but ARQ enqueue throws.
    The outbox relay safety net must pick up the row — the route must not raise.
    """
    db = _make_db(scalar_return=None)
    arq_pool = AsyncMock()
    arq_pool.enqueue_job = AsyncMock(side_effect=ConnectionError("redis down"))
    req = _make_request(arq_pool=arq_pool)
    upload = _make_upload_mock(b"content")
    mock_session, _ = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    import json
    body = json.loads(resp.body)
    # Still returns 202 queued — the outbox entry remains unpublished for the relay
    assert resp.status_code == 202
    assert body["data"]["status"] == "queued"
    arq_pool.enqueue_job.assert_awaited_once()


# ---------------------------------------------------------------------------
# Backpressure (Phase 2c)
# ---------------------------------------------------------------------------

async def test_upload_returns_429_when_queue_full():
    """Upload is rejected with 429 when the CPU queue depth exceeds MAX_QUEUE_DEPTH."""
    db = _make_db(scalar_return=None)
    arq_pool = AsyncMock()
    # queue_depth=100 == MAX_QUEUE_DEPTH=100, so >= triggers the guard
    req = _make_request(arq_pool=arq_pool, queue_depth=100)
    upload = _make_upload_mock(b"some file content")
    mock_session, _ = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        from fastapi import HTTPException
        try:
            await upload_document(request=req, file=upload, db=db, auth=_make_auth())
            assert False, "Expected HTTPException 429"
        except HTTPException as exc:
            assert exc.status_code == 429


# ---------------------------------------------------------------------------
# POST /documents/{id}/retry
# ---------------------------------------------------------------------------

def _make_failed_doc(doc_id: uuid.UUID | None = None, with_payload: bool = True) -> MagicMock:
    doc = _make_doc(doc_id)
    doc.status = "failed"
    job_kwargs = {
        "document_id": str(doc.id),
        "mime": "text/plain",
        "size": 100,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "config_snapshot": {},
    }
    doc.error = (
        {"reason": "extraction error", "retry_payload": {"task": "ingest_dispatch", "kwargs": job_kwargs}}
        if with_payload
        else {"reason": "crashed before enqueue"}
    )
    return doc


async def test_retry_document_returns_202_queued():
    doc_id = uuid.uuid4()
    doc = _make_failed_doc(doc_id)
    db = _make_db(get_return=doc)
    req = _make_request(arq_pool=AsyncMock())

    import json
    resp = await retry_document(document_id=doc_id, request=req, db=db, auth=_make_auth())
    body = json.loads(resp.body)

    assert resp.status_code == 202
    assert body["success"] is True
    assert body["data"]["status"] == "queued"
    assert body["data"]["document_id"] == str(doc_id)
    assert "poll_url" in body["data"]


async def test_retry_document_resets_status_to_queued():
    doc = _make_failed_doc()
    db = _make_db(get_return=doc)
    req = _make_request(arq_pool=AsyncMock())

    await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    assert doc.status == "queued"
    assert doc.error is None
    db.commit.assert_awaited()


async def test_retry_document_enqueues_job():
    doc = _make_failed_doc()
    arq_pool = AsyncMock()
    db = _make_db(get_return=doc)
    req = _make_request(arq_pool=arq_pool)

    await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    arq_pool.enqueue_job.assert_awaited_once()
    call_args = arq_pool.enqueue_job.call_args
    assert call_args[0][0] == "ingest_dispatch"


async def test_retry_document_404_when_not_found():
    db = _make_db(get_return=None)
    req = _make_request()

    with pytest.raises(HTTPException) as exc_info:
        await retry_document(document_id=uuid.uuid4(), request=req, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 404


async def test_retry_document_409_when_not_failed():
    doc = _make_doc()  # status="indexed"
    db = _make_db(get_return=doc)
    req = _make_request()

    with pytest.raises(HTTPException) as exc_info:
        await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 409
    assert "indexed" in exc_info.value.detail


async def test_retry_document_404_when_no_retry_payload():
    doc = _make_failed_doc(with_payload=False)
    db = _make_db(get_return=doc)
    req = _make_request()

    with pytest.raises(HTTPException) as exc_info:
        await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 404


async def test_retry_document_no_arq_pool_still_queues_in_db():
    """No ARQ pool: document status is reset even if enqueue is skipped."""
    doc = _make_failed_doc()
    db = _make_db(get_return=doc)
    req = _make_request(arq_pool=None)

    resp = await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    assert resp.status_code == 202
    assert doc.status == "queued"


async def test_retry_document_writes_outbox_row():
    """Status reset and Outbox row are committed atomically so outbox_relay can replay."""
    doc = _make_failed_doc()
    db = _make_db(get_return=doc)
    req = _make_request(arq_pool=AsyncMock())

    await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    # db.add() must be called at least once (for the Outbox row)
    db.add.assert_called()


async def test_retry_document_arq_enqueue_failure_outbox_provides_safety_net():
    """On Redis failure the doc stays queued and the outbox row (unpublished) remains
    for the outbox_relay cron to pick up — same safety net as the upload path."""
    doc = _make_failed_doc()
    arq_pool = AsyncMock()
    arq_pool.enqueue_job = AsyncMock(side_effect=ConnectionError("redis down"))
    db = _make_db(get_return=doc)
    req = _make_request(arq_pool=arq_pool)

    resp = await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    assert resp.status_code == 202
    assert doc.status == "queued"
    # Outbox row was written (db.add called for the Outbox entry)
    db.add.assert_called()


async def test_retry_document_422_on_malformed_payload():
    """Malformed retry_payload (unknown task name) raises 422 before touching the DB."""
    doc = _make_failed_doc()
    doc.error = {"reason": "crash", "retry_payload": {"task": "arbitrary_task", "kwargs": {}}}
    db = _make_db(get_return=doc)
    req = _make_request()

    with pytest.raises(HTTPException) as exc_info:
        await retry_document(document_id=doc.id, request=req, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 422
    db.commit.assert_not_called()  # DB must not be touched before validation


async def test_upload_proceeds_when_queue_below_limit():
    """Upload is accepted when the CPU queue has capacity."""
    db = _make_db(scalar_return=None)
    arq_pool = AsyncMock()
    req = _make_request(arq_pool=arq_pool, queue_depth=99)  # one below MAX_QUEUE_DEPTH=100
    upload = _make_upload_mock(b"some file content")
    mock_session, _ = _mock_s3_session()

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# GPU backpressure (Phase 2c P2)
# ---------------------------------------------------------------------------

def _make_request_with_gpu_depth(cpu_depth: int = 0, gpu_depth: int = 0) -> MagicMock:
    arq_pool = AsyncMock()
    arq_pool.default_queue_name = "arq:queue"
    arq_pool.zcard = AsyncMock(side_effect=lambda key: {
        "arq:queue": cpu_depth,
        "arq:gpu": gpu_depth,
    }.get(key, 0))
    req = MagicMock()
    req.app.state.arq_pool = arq_pool
    return req


async def test_upload_returns_429_when_gpu_queue_full():
    """GPU file upload rejected with 429 when arq:gpu depth >= MAX_GPU_QUEUE_DEPTH."""
    db = _make_db(scalar_return=None)
    req = _make_request_with_gpu_depth(cpu_depth=0, gpu_depth=20)  # == MAX_GPU_QUEUE_DEPTH
    upload = _make_upload_mock(b"fake audio data", filename="audio.mp3")
    mock_session, _ = _mock_s3_session()

    gpu_handler = MagicMock()
    gpu_handler.cost_class = "gpu"

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="audio/mpeg"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
        patch("omnivore.api.routes.documents.registry.resolve", return_value=gpu_handler),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 429


async def test_upload_proceeds_when_gpu_queue_below_limit():
    """GPU file upload accepted when arq:gpu depth < MAX_GPU_QUEUE_DEPTH."""
    db = _make_db(scalar_return=None)
    req = _make_request_with_gpu_depth(cpu_depth=0, gpu_depth=19)  # one below limit
    upload = _make_upload_mock(b"fake audio data", filename="audio.mp3")
    mock_session, _ = _mock_s3_session()

    gpu_handler = MagicMock()
    gpu_handler.cost_class = "gpu"

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="audio/mpeg"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
        patch("omnivore.api.routes.documents.registry.resolve", return_value=gpu_handler),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    assert resp.status_code == 202


async def test_cpu_file_not_checked_against_gpu_queue():
    """Non-GPU file upload is not rejected even when the GPU queue is completely full."""
    db = _make_db(scalar_return=None)
    req = _make_request_with_gpu_depth(cpu_depth=0, gpu_depth=999)  # GPU queue swamped
    upload = _make_upload_mock(b"plain text", filename="doc.txt")
    mock_session, _ = _mock_s3_session()

    cpu_handler = MagicMock()
    cpu_handler.cost_class = "cpu"

    with (
        patch("omnivore.api.routes.documents.check_rate_limit", AsyncMock(return_value=_rl_allowed())),
        patch("omnivore.api.routes.documents.magic.from_buffer", return_value="text/plain"),
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
        patch("omnivore.api.routes.documents.registry.resolve", return_value=cpu_handler),
    ):
        resp = await upload_document(request=req, file=upload, db=db, auth=_make_auth())

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Phase 3: get_document — summary + routing_decision fields
# ---------------------------------------------------------------------------

async def test_get_document_includes_summary_field():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.doc_metadata = {"summary": {"title": "Test", "abstract": "A doc.", "key_points": [], "topics": []}}
    doc.routing_decision = {"policy": "default", "sink_counts": {"vector": 3}}
    db = _make_db(get_return=doc)

    resp = await get_document(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.success is True
    assert resp.data["summary"]["title"] == "Test"
    assert resp.data["routing_decision"]["policy"] == "default"


async def test_get_document_summary_none_when_absent():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.doc_metadata = {}
    doc.routing_decision = None
    db = _make_db(get_return=doc)

    resp = await get_document(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.data["summary"] is None
    assert resp.data["routing_decision"] is None


# ---------------------------------------------------------------------------
# Phase 3: get_document_entities endpoint
# ---------------------------------------------------------------------------

def _make_entity(label: str, value: str, normalized: str, confidence: float = 1.0) -> MagicMock:
    e = MagicMock()
    e.label = label
    e.value = value
    e.normalized = normalized
    e.confidence = confidence
    return e


async def test_get_entities_returns_list():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    entities = [
        _make_entity("ORG", "Apple Inc", "apple inc"),
        _make_entity("GPE", "California", "california"),
    ]
    db = _make_db(get_return=doc, scalars_return=entities)

    resp = await get_document_entities(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.success is True
    assert len(resp.data) == 2
    assert resp.meta["count"] == 2
    assert resp.data[0]["label"] == "ORG"
    assert resp.data[0]["value"] == "Apple Inc"


async def test_get_entities_returns_404_for_missing_document():
    doc_id = uuid.uuid4()
    db = _make_db(get_return=None)

    with pytest.raises(HTTPException) as exc_info:
        await get_document_entities(document_id=doc_id, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 404


async def test_get_entities_returns_empty_list_when_no_entities():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    db = _make_db(get_return=doc, scalars_return=[])

    resp = await get_document_entities(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.success is True
    assert resp.data == []
    assert resp.meta["count"] == 0


# ---------------------------------------------------------------------------
# DELETE /documents/{document_id}
# ---------------------------------------------------------------------------

def _make_db_with_delete(doc=None) -> AsyncMock:
    db = _make_db(get_return=doc)
    db.delete = AsyncMock()
    return db


async def test_delete_document_returns_200():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.status = "indexed"
    db = _make_db_with_delete(doc)
    mock_session, mock_s3 = _mock_s3_session()
    mock_s3.delete_object = AsyncMock()

    with (
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await delete_document(document_id=doc_id, db=db, auth=_make_auth())

    assert resp.success is True
    assert resp.data["document_id"] == str(doc_id)
    assert resp.data["deleted"] is True
    db.delete.assert_awaited_once_with(doc)
    db.commit.assert_awaited_once()


async def test_delete_document_not_found_returns_404():
    db = _make_db_with_delete(doc=None)

    with pytest.raises(HTTPException) as exc_info:
        await delete_document(document_id=uuid.uuid4(), db=db, auth=_make_auth())

    assert exc_info.value.status_code == 404


async def test_delete_document_in_progress_returns_409():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.status = "extracting"
    db = _make_db_with_delete(doc)

    with pytest.raises(HTTPException) as exc_info:
        await delete_document(document_id=doc_id, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 409
    assert "currently being processed" in exc_info.value.detail
    db.delete.assert_not_called()


async def test_delete_document_enriching_returns_409():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.status = "enriching"
    db = _make_db_with_delete(doc)

    with pytest.raises(HTTPException) as exc_info:
        await delete_document(document_id=doc_id, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 409


async def test_delete_document_routing_returns_409():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.status = "routing"
    db = _make_db_with_delete(doc)

    with pytest.raises(HTTPException) as exc_info:
        await delete_document(document_id=doc_id, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 409


async def test_delete_document_s3_failure_is_best_effort():
    """S3 delete failure must not block the DB delete."""
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.status = "failed"
    db = _make_db_with_delete(doc)
    mock_session, mock_s3 = _mock_s3_session()
    mock_s3.delete_object = AsyncMock(side_effect=Exception("NoSuchKey"))

    with (
        patch("omnivore.api.routes.documents.get_settings", return_value=_mock_settings()),
        patch("omnivore.api.routes.documents.aioboto3.Session", return_value=mock_session),
    ):
        resp = await delete_document(document_id=doc_id, db=db, auth=_make_auth())

    # DB delete still proceeds even though S3 threw
    assert resp.success is True
    db.delete.assert_awaited_once_with(doc)


async def test_delete_document_wrong_tenant_returns_404():
    """Document belonging to a different tenant must return 404."""
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    doc.tenant_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")  # different tenant
    db = _make_db_with_delete(doc)

    with pytest.raises(HTTPException) as exc_info:
        await delete_document(document_id=doc_id, db=db, auth=_make_auth())

    assert exc_info.value.status_code == 404
