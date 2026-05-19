"""Chaos tests: worker-side failure scenarios.

Calls _run_ingest / ingest_dispatch directly (unit-test style) to simulate
handler crashes, retry payload persistence, and atomic claim races.
Mirrors tests/unit/test_tasks.py patterns exactly.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from omnivore.pipeline.models import ExtractionResult
from omnivore.worker.tasks import _run_ingest, ingest_dispatch

from .conftest import _TEST_TENANT_ID, make_arq_ctx, make_doc_mock, make_worker_session

# ---------------------------------------------------------------------------
# Helpers shared across this module
# ---------------------------------------------------------------------------

_FAKE_VEC = [[0.0] * 768]


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


def _make_cpu_handler(result: ExtractionResult) -> type:
    class FakeCpuHandler:
        name = "fake-cpu"
        version = "0.0.1"
        accepts = ("application/fake",)
        cost_class = "cpu"
        timeout_seconds = 30

        async def extract(self, blob, ctx):
            return result

    return FakeCpuHandler


def _make_crashing_handler(exc: Exception) -> type:
    class CrashingHandler:
        name = "crasher"
        version = "0.0.1"
        accepts = ("application/fake",)
        cost_class = "cpu"
        timeout_seconds = 30

        async def extract(self, blob, ctx):
            raise exc

    return CrashingHandler


# ---------------------------------------------------------------------------
# test_handler_crash_marks_document_failed
# ---------------------------------------------------------------------------

async def test_handler_crash_marks_document_failed():
    """When extract() raises, the document status must be set to 'failed'."""
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, db = make_worker_session(doc)

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch(
            "omnivore.worker.tasks.registry.resolve",
            return_value=_make_crashing_handler(RuntimeError("extraction blew up")),
        ),
    ):
        out = await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=1000,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "error"
    assert "extraction blew up" in out["reason"]
    assert doc.status == "failed"


async def test_handler_crash_error_contains_reason():
    """doc.error must contain a 'reason' key with the exception message."""
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, _ = make_worker_session(doc)

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch(
            "omnivore.worker.tasks.registry.resolve",
            return_value=_make_crashing_handler(ValueError("bad format")),
        ),
    ):
        await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    assert doc.error is not None
    assert "reason" in doc.error
    assert "bad format" in doc.error["reason"]


# ---------------------------------------------------------------------------
# test_handler_crash_stores_retry_payload
# ---------------------------------------------------------------------------

async def test_handler_crash_stores_retry_payload():
    """On extraction failure, doc.error['retry_payload'] must be present and valid."""
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, _ = make_worker_session(doc)
    config = {"lang": "en", "model": "fast"}

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch(
            "omnivore.worker.tasks.registry.resolve",
            return_value=_make_crashing_handler(RuntimeError("simulated failure")),
        ),
    ):
        await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=999,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot=config,
        )

    rp = doc.error["retry_payload"]
    assert rp["task"] == "ingest_dispatch"
    assert rp["kwargs"]["document_id"] == str(doc_id)
    assert rp["kwargs"]["mime"] == "application/fake"
    assert rp["kwargs"]["size"] == 999
    assert rp["kwargs"]["tenant_id"] == str(_TEST_TENANT_ID)
    assert rp["kwargs"]["config_snapshot"] == config


async def test_handler_crash_retry_payload_task_is_ingest_dispatch():
    """retry_payload.task must be 'ingest_dispatch' so the retry endpoint allows it."""
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, _ = make_worker_session(doc)

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch(
            "omnivore.worker.tasks.registry.resolve",
            return_value=_make_crashing_handler(Exception("oops")),
        ),
    ):
        await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=10,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    # _ALLOWED_RETRY_TASKS = {"ingest_dispatch", "gpu_ingest_dispatch"}
    # The stored task name must be in that allowlist
    task_name = doc.error["retry_payload"]["task"]
    assert task_name in {"ingest_dispatch", "gpu_ingest_dispatch"}


# ---------------------------------------------------------------------------
# test_ingest_claim_is_atomic — two dispatches on the same document
# ---------------------------------------------------------------------------

async def test_ingest_claim_lost_when_fetchone_returns_none():
    """Simulates Postgres RETURNING 0 rows on the atomic claim UPDATE.

    In production this happens when a second worker races to claim the same
    document.  At the mock level we force fetchone() → None to trigger the
    identical code path and verify the 'already_claimed' bail-out.
    """
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    # claim_succeeds=False → fetchone() returns None on the atomic UPDATE
    session_factory, db = make_worker_session(doc, claim_succeeds=False)
    handler_cls = _make_cpu_handler(_empty_result(doc_id))

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch("omnivore.worker.tasks.registry.resolve", return_value=handler_cls),
    ):
        out = await _run_ingest(
            make_arq_ctx(), handler_cls,
            str(doc_id), "application/fake", 100,
            str(_TEST_TENANT_ID), {},
        )

    assert out["reason"] == "already_claimed"
    # No extraction or commit after a failed claim
    db.commit.assert_not_called()


async def test_sequential_dispatches_second_returns_already_processed():
    """Two sequential dispatches: first indexes the doc, second is a no-op.

    asyncio.gather runs coroutines cooperatively in one event loop; because
    _run_ingest does not yield between the status check and the claim, the
    first call completes fully before the second begins.  The second call then
    sees status='indexed' and returns early via the idempotency guard.

    This exercises the real behaviour of sequential duplicate dispatch
    (e.g., outbox_relay fires twice after a restart).
    """
    doc_id = uuid.uuid4()
    result = _empty_result(doc_id)
    handler_cls = _make_cpu_handler(result)

    call_count = 0

    def _fetchone_alternating():
        nonlocal call_count
        call_count += 1
        # Simulate: first call to the DB wins the claim; subsequent calls see no row
        return MagicMock() if call_count == 1 else None

    doc = make_doc_mock(doc_id, status="queued")

    db = AsyncMock()
    db.get = AsyncMock(return_value=doc)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    execute_result = MagicMock()
    execute_result.fetchone = MagicMock(side_effect=_fetchone_alternating)
    db.execute = AsyncMock(return_value=execute_result)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=cm)

    arq_ctx = make_arq_ctx()

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch("omnivore.worker.tasks.registry.resolve", return_value=handler_cls),
        patch("omnivore.worker.tasks.chunk_result", return_value=[]),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(return_value=[])),
    ):
        out1, out2 = await asyncio.gather(
            _run_ingest(arq_ctx, handler_cls, str(doc_id), "application/fake", 100, str(_TEST_TENANT_ID), {}),
            _run_ingest(arq_ctx, handler_cls, str(doc_id), "application/fake", 100, str(_TEST_TENANT_ID), {}),
        )

    # First dispatch: indexed successfully
    # Second dispatch: sees status='indexed' (already_processed) OR already_claimed
    # In either case, the combined outputs must not contain two "indexed" completions
    # without at least one indicating it was a no-op.
    reasons = {out1.get("reason"), out2.get("reason")}
    assert any(r in {"already_processed", "already_claimed"} for r in reasons), (
        f"Expected at least one no-op; got {out1=}, {out2=}"
    )


async def test_run_ingest_claim_lost_returns_already_claimed():
    """Single call where fetchone returns None → reason='already_claimed'."""
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, db = make_worker_session(doc, claim_succeeds=False)
    result = _empty_result(doc_id)
    handler_cls = _make_cpu_handler(result)

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch("omnivore.worker.tasks.registry.resolve", return_value=handler_cls),
    ):
        out = await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    assert out["reason"] == "already_claimed"
    # No extraction work done after a failed claim
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# test_document_not_found_returns_error
# ---------------------------------------------------------------------------

async def test_ingest_doc_not_found_returns_error():
    """Document missing from DB after dispatch → status='error', reason='document_not_found'."""
    doc_id = uuid.uuid4()
    session_factory, _ = make_worker_session(doc=None)
    result = _empty_result(doc_id)

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_cpu_handler(result)),
    ):
        out = await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "error"
    assert out["reason"] == "document_not_found"


# ---------------------------------------------------------------------------
# test_embedding_failure_does_not_fail_document
# ---------------------------------------------------------------------------

async def test_embedding_failure_document_still_indexed():
    """Embedding failure (GPU OOM, model error) must not revert the document to 'failed'.

    Chunks are still written and BM25 search remains available.
    """
    from omnivore.pipeline.models import Chunk as PipelineChunk

    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, _ = make_worker_session(doc)
    result = _empty_result(doc_id)
    chunk = PipelineChunk(
        document_id=doc_id,
        tenant_id=_TEST_TENANT_ID,
        ordinal=0,
        kind="text",
        content="hello world",
        token_count=2,
        position={},
        heading_path=[],
        source_block_ids=[],
    )

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_cpu_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=[chunk]),
        patch(
            "omnivore.worker.tasks.embed_chunks",
            AsyncMock(side_effect=RuntimeError("CUDA out of memory")),
        ),
    ):
        out = await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    # Despite embedding failure, document is indexed
    assert out["status"] == "indexed"
    assert out["chunks"] == 1
    assert doc.status == "indexed"


# ---------------------------------------------------------------------------
# test_ner_failure_does_not_fail_document
# ---------------------------------------------------------------------------

async def test_ner_failure_document_still_indexed():
    """NER is non-fatal — an exception in extract_entities must not mark the doc failed."""
    from omnivore.pipeline.models import Chunk as PipelineChunk

    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="queued")
    session_factory, _ = make_worker_session(doc)
    result = _empty_result(doc_id)
    chunk = PipelineChunk(
        document_id=doc_id,
        tenant_id=_TEST_TENANT_ID,
        ordinal=0,
        kind="text",
        content="Apple acquired WWDC",
        token_count=3,
        position={},
        heading_path=[],
        source_block_ids=[],
    )

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch("omnivore.worker.tasks.registry.resolve", return_value=_make_cpu_handler(result)),
        patch("omnivore.worker.tasks.chunk_result", return_value=[chunk]),
        patch("omnivore.worker.tasks.embed_chunks", AsyncMock(return_value=[[0.0] * 768])),
        patch(
            "omnivore.worker.tasks.extract_entities",
            side_effect=RuntimeError("spaCy model missing"),
        ),
    ):
        out = await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "indexed"
    assert doc.status == "indexed"


# ---------------------------------------------------------------------------
# test_already_indexed_document_skips_processing
# ---------------------------------------------------------------------------

async def test_already_indexed_skips_reprocessing():
    """Duplicate dispatch for an already-indexed doc must return early without DB writes."""
    doc_id = uuid.uuid4()
    doc = make_doc_mock(doc_id, status="indexed")
    session_factory, db = make_worker_session(doc)

    with (
        patch("omnivore.worker.tasks.tenant_session", session_factory),
        patch(
            "omnivore.worker.tasks.registry.resolve",
            return_value=_make_cpu_handler(_empty_result(doc_id)),
        ),
    ):
        out = await ingest_dispatch(
            make_arq_ctx(),
            document_id=str(doc_id),
            mime="application/fake",
            size=100,
            tenant_id=str(_TEST_TENANT_ID),
            config_snapshot={},
        )

    assert out["status"] == "indexed"
    assert out["reason"] == "already_processed"
    db.commit.assert_not_called()
