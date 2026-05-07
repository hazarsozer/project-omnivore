"""Unit tests for worker/tasks.py — ingest_dispatch + outbox_relay."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from omnivore.pipeline.models import Chunk as PipelineChunk
from omnivore.pipeline.models import ExtractionResult
from omnivore.worker.tasks import ingest_dispatch, outbox_relay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arq_ctx() -> dict:
    return {"redis": AsyncMock(), "job_id": "test-job"}


def _make_doc(doc_id: uuid.UUID) -> MagicMock:
    doc = MagicMock()
    doc.id = doc_id
    doc.filename = "test.docx"
    doc.storage_uri = "s3://bucket/raw/file.docx"
    doc.status = "queued"
    doc.handler_name = None
    doc.handler_version = None
    doc.doc_metadata = {}
    doc.error = None
    return doc


def _empty_result(doc_id: uuid.UUID) -> ExtractionResult:
    return ExtractionResult(
        document_id=doc_id,
        source_handler="fake",
        handler_version="0.0.1",
        fragments=[],
        blocks=[],
        tables=[],
        metadata={"format": "fake"},
    )


def _make_fake_handler(result: ExtractionResult) -> type:
    class FakeHandler:
        name = "fake"
        version = "0.0.1"
        accepts = ("application/fake",)
        cost_class = "cpu"
        timeout_seconds = 30

        async def extract(self, blob, ctx):
            return result

    return FakeHandler


def _make_session_ctx(doc=None):
    db = AsyncMock()
    db.get = AsyncMock(return_value=doc)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_local = MagicMock(return_value=cm)
    return session_local, db


def _one_chunk(doc_id: uuid.UUID, tenant_id: uuid.UUID) -> list[PipelineChunk]:
    return [
        PipelineChunk(
            document_id=doc_id,
            tenant_id=tenant_id,
            ordinal=0,
            kind="text",
            content="hello world",
            token_count=2,
            position={},
            heading_path=[],
            source_block_ids=[],
        )
    ]


_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FAKE_VEC = [[0.0] * 768]


# ---------------------------------------------------------------------------
# ingest_dispatch — happy path
# ---------------------------------------------------------------------------

async def test_ingest_dispatch_success_status_indexed():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, db = _make_session_ctx(doc)
    result = _empty_result(doc_id)
    chunks = _one_chunk(doc_id, _TENANT_ID)

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_fake_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=chunks),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(return_value=_FAKE_VEC)),
    ):
        out = await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=1000,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "indexed"
    assert out["document_id"] == str(doc_id)
    assert out["chunks"] == 1


async def test_ingest_dispatch_commits_at_least_twice():
    """Persist-then-embed pattern: one commit for status+chunks, one for embeddings."""
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, db = _make_session_ctx(doc)
    result = _empty_result(doc_id)
    chunks = _one_chunk(doc_id, _TENANT_ID)

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_fake_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=chunks),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(return_value=_FAKE_VEC)),
    ):
        await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert db.commit.await_count >= 2


async def test_ingest_dispatch_adds_chunk_rows():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, db = _make_session_ctx(doc)
    result = _empty_result(doc_id)
    chunks = _one_chunk(doc_id, _TENANT_ID)

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_fake_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=chunks),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(return_value=_FAKE_VEC)),
    ):
        await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert db.add.called


# ---------------------------------------------------------------------------
# ingest_dispatch — document not found
# ---------------------------------------------------------------------------

async def test_ingest_dispatch_doc_not_found():
    doc_id = uuid.uuid4()
    session_local, _ = _make_session_ctx(doc=None)

    with patch("omnivore.worker.tasks.AsyncSessionLocal", session_local):
        out = await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "error"
    assert out["reason"] == "document_not_found"


# ---------------------------------------------------------------------------
# ingest_dispatch — no handler for MIME type
# ---------------------------------------------------------------------------

async def test_ingest_dispatch_no_handler():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, db = _make_session_ctx(doc)

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=None),
    ):
        out = await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/unknown",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "error"
    assert out["reason"] == "no_handler"
    assert doc.status == "failed"


# ---------------------------------------------------------------------------
# ingest_dispatch — handler extract raises
# ---------------------------------------------------------------------------

async def test_ingest_dispatch_handler_extract_fails():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, _ = _make_session_ctx(doc)

    class BrokenHandler:
        name = "broken"
        version = "1.0.0"
        accepts = ("application/broken",)
        cost_class = "cpu"
        timeout_seconds = 30

        async def extract(self, blob, ctx):
            raise ValueError("parse error")

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=BrokenHandler),
    ):
        out = await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/broken",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "error"
    assert "parse error" in out["reason"]
    assert doc.status == "failed"


# ---------------------------------------------------------------------------
# ingest_dispatch — embedding failure: chunks still indexed (BM25 available)
# ---------------------------------------------------------------------------

async def test_ingest_dispatch_embed_fails_status_still_indexed():
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, db = _make_session_ctx(doc)
    result = _empty_result(doc_id)
    chunks = _one_chunk(doc_id, _TENANT_ID)

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_fake_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=chunks),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(side_effect=RuntimeError("GPU OOM"))),
    ):
        out = await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "indexed"
    assert out["chunks"] == 1


# ---------------------------------------------------------------------------
# outbox_relay — empty queue
# ---------------------------------------------------------------------------

async def test_ingest_dispatch_persists_extracted_tables():
    """When the handler returns StructuredTable objects they must be flushed to DB."""
    doc_id = uuid.uuid4()
    doc = _make_doc(doc_id)
    session_local, db = _make_session_ctx(doc)

    from omnivore.pipeline.models import StructuredTable
    result = ExtractionResult(
        document_id=doc_id,
        source_handler="fake",
        handler_version="0.0.1",
        fragments=[],
        blocks=[],
        tables=[
            StructuredTable(
                name="table_1",
                headers=["Name", "Value"],
                rows=[{"Name": "foo", "Value": "bar"}],
            )
        ],
        metadata={"format": "fake"},
    )

    with (
        patch("omnivore.worker.tasks.AsyncSessionLocal", session_local),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_fake_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=_one_chunk(doc_id, _TENANT_ID)),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(return_value=_FAKE_VEC)),
    ):
        out = await ingest_dispatch(
            _arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "indexed"
    # flush is called at least once (for ExtractedTable.id population)
    db.flush.assert_awaited()


# ---------------------------------------------------------------------------
# outbox_relay — empty queue
# ---------------------------------------------------------------------------

async def test_outbox_relay_empty_queue():
    db = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=[])
    db.scalars = AsyncMock(return_value=scalars_result)
    db.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_local = MagicMock(return_value=cm)

    redis = AsyncMock()

    with patch("omnivore.worker.tasks.AsyncSessionLocal", session_local):
        result = await outbox_relay({"redis": redis})

    assert result == {"published": 0}
    redis.enqueue_job.assert_not_called()


# ---------------------------------------------------------------------------
# outbox_relay — publishes pending rows
# ---------------------------------------------------------------------------

async def test_outbox_relay_publishes_unpublished_rows():
    row1 = MagicMock()
    row1.id = 1
    row1.payload = {"task": "ingest_dispatch", "kwargs": {"document_id": str(uuid.uuid4())}}
    row1.published_at = None

    row2 = MagicMock()
    row2.id = 2
    row2.payload = {"task": "ingest_dispatch", "kwargs": {"document_id": str(uuid.uuid4())}}
    row2.published_at = None

    db = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=[row1, row2])
    db.scalars = AsyncMock(return_value=scalars_result)
    db.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_local = MagicMock(return_value=cm)

    redis = AsyncMock()
    redis.enqueue_job = AsyncMock()

    with patch("omnivore.worker.tasks.AsyncSessionLocal", session_local):
        result = await outbox_relay({"redis": redis})

    assert result == {"published": 2}
    assert redis.enqueue_job.await_count == 2
    assert row1.published_at is not None
    assert row2.published_at is not None


async def test_outbox_relay_handles_enqueue_failure_gracefully():
    """A single enqueue failure should not prevent other rows from being published."""
    row_good = MagicMock()
    row_good.id = 1
    row_good.payload = {"task": "ingest_dispatch", "kwargs": {"document_id": str(uuid.uuid4())}}
    row_good.published_at = None

    row_bad = MagicMock()
    row_bad.id = 2
    row_bad.payload = {"task": "ingest_dispatch", "kwargs": {"document_id": str(uuid.uuid4())}}
    row_bad.published_at = None

    db = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=[row_good, row_bad])
    db.scalars = AsyncMock(return_value=scalars_result)
    db.commit = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_local = MagicMock(return_value=cm)

    call_count = 0

    async def enqueue_side_effect(task, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # success
        raise ConnectionError("redis down")

    redis = AsyncMock()
    redis.enqueue_job = enqueue_side_effect

    with patch("omnivore.worker.tasks.AsyncSessionLocal", session_local):
        result = await outbox_relay({"redis": redis})

    # Only the first row succeeds; second fails gracefully
    assert result == {"published": 1}
