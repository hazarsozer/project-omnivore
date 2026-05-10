"""E2E pipeline test: upload → ARQ queue → ingest_dispatch → search.

Requires the docker-compose stack to be running:
    docker compose up -d postgres redis minio

What this test proves that prior unit/E2E tests could not:
- The API's arq pool and the worker's WorkerSettings.queue_name both resolve to
  the same Redis key ("arq:queue").  The Phase 2c audit found this was broken from
  Phase 0 — the two sides disagreed on the queue name and jobs were silently dropped.
- The full extraction path (MinIO read → handler → chunker → embeddings → postgres)
  produces a searchable result without errors.

embed_chunks is patched to return deterministic unit vectors so the test runs without
downloading the ~440 MB BGE model and completes in < 5 s.

The test document gets a unique UUID in its body so the SHA-256 dedup guard never
triggers, keeping each run independent.
"""
from __future__ import annotations

import asyncio
import io
import math
import uuid
from unittest.mock import patch

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from omnivore.api.main import app
from omnivore.config import get_settings
from omnivore.constants import DEFAULT_TENANT_ID
from omnivore.db.models import Chunk as ChunkRow
from omnivore.db.models import Document, Entity
from omnivore.db.session import AsyncSessionLocal
from omnivore.pipeline.embeddings import EMBEDDING_DIM
from omnivore.pipeline.registry import registry
from omnivore.worker.tasks import ingest_dispatch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(seed: int) -> list[float]:
    """Deterministic L2-normalised vector, one per seed value."""
    v = [math.sin(seed + i * 0.37) for i in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


async def _fake_embed(chunks, **_kwargs) -> list[list[float]]:
    """Return one deterministic unit vector per chunk — no model download."""
    return [_unit_vec(i) for i in range(len(chunks))]


def _build_sample(run_id: str) -> bytes:
    """Each call gets a unique body so SHA-256 dedup never fires."""
    return f"""# Pipeline E2E Test Document — {run_id}

This document exercises the full Omnivore ingestion path.

## Overview

The pipeline accepts any file, stores it in MinIO, and enqueues an ARQ job.
A worker picks up the job, runs the appropriate handler, chunks the result,
embeds the chunks, and writes everything to PostgreSQL.

## Key Components

FastAPI handles the upload and queues the job via arq.create_pool.
The worker reads from the same queue using WorkerSettings.queue_name.
Both must resolve to the same Redis key — this is the wiring the test verifies.
""".encode()


# ---------------------------------------------------------------------------
# Module-scoped fixture: run the full pipeline once, return captured results
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pipeline_results():
    """Upload a document, run ingest_dispatch, return collected observations."""
    run_id = str(uuid.uuid4())
    sample = _build_sample(run_id)
    results: dict = {}

    async def _run() -> None:
        settings = get_settings()

        # Bootstrap what the lifespan normally provides.
        # ASGITransport does not fire the ASGI lifespan, so we set up the two
        # dependencies the route and worker need: the handler registry and the
        # ARQ pool on app.state.
        registry.discover()
        pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        app.state.arq_pool = pool

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Step 1 — upload -------------------------------------------------
                resp = await client.post(
                    "/v1/documents",
                    files={"file": ("e2e_pipeline.txt", io.BytesIO(sample), "text/plain")},
                )
                results["upload_status_code"] = resp.status_code
                results["upload_body"] = resp.json()

                if resp.status_code != 202:
                    return

                doc_id: str = resp.json()["data"]["document_id"]
                results["doc_id"] = doc_id

                # Step 2 — queue depth check before worker runs -------------------
                # The upload route enqueues to pool.default_queue_name ("arq:queue").
                # WorkerSettings.queue_name must match or jobs are silently lost.
                results["queue_depth_after_upload"] = await pool.zcard("arq:queue")

                # Step 3 — run ingest_dispatch (simulates the worker) -------------
                ctx: dict = {"redis": pool}
                with patch(
                    "omnivore.worker.tasks.embed_chunks", side_effect=_fake_embed
                ):
                    ingest_result = await ingest_dispatch(
                        ctx,
                        document_id=doc_id,
                        mime="text/plain",
                        size=len(sample),
                        tenant_id=str(DEFAULT_TENANT_ID),
                        config_snapshot={},
                    )
                results["ingest_result"] = ingest_result

                # Step 3b — Phase 3 enrichment assertions (DB queries) ----------
                doc_uuid = uuid.UUID(doc_id)
                async with AsyncSessionLocal() as db:
                    chunks_q = (
                        await db.scalars(
                            select(ChunkRow).where(ChunkRow.document_id == doc_uuid)
                        )
                    ).all()
                    results["chunk_languages"] = [c.language for c in chunks_q]

                    entities_q = (
                        await db.scalars(
                            select(Entity).where(Entity.document_id == doc_uuid)
                        )
                    ).all()
                    results["entity_count"] = len(entities_q)
                    results["entity_labels"] = [e.label for e in entities_q]

                    doc_row = await db.get(Document, doc_uuid)
                    results["routing_decision"] = doc_row.routing_decision if doc_row else None

                # Step 4 — GET document status ------------------------------------
                status_resp = await client.get(f"/v1/documents/{doc_id}")
                results["status_code"] = status_resp.status_code
                results["doc_data"] = status_resp.json().get("data", {})

                # Step 5 — search -------------------------------------------------
                search_resp = await client.post(
                    "/v1/search",
                    json={
                        "query": "ARQ queue ingestion pipeline worker",
                        "mode": "bm25",
                        "top_k": 10,
                    },
                )
                results["search_status_code"] = search_resp.status_code
                results["search_results"] = search_resp.json().get("data", [])
        finally:
            await pool.aclose()
            # Remove from app.state so it doesn't leak between test modules
            if hasattr(app.state, "arq_pool"):
                del app.state.arq_pool

    asyncio.run(_run())
    yield results

    # Teardown — remove test document and its chunks from the dev database.
    # A fresh engine must be created here because AsyncSessionLocal's engine was
    # initialised inside asyncio.run(_run()), which closed that event loop.
    # Reusing those connections in a new loop raises "Future attached to a different loop".
    doc_id_str = results.get("doc_id")
    if not doc_id_str:
        return

    async def _cleanup() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        clean_engine = create_async_engine(get_settings().DATABASE_URL)
        CleanSession = async_sessionmaker(clean_engine, expire_on_commit=False)
        doc_uuid = uuid.UUID(doc_id_str)
        async with CleanSession() as db:
            await db.execute(delete(ChunkRow).where(ChunkRow.document_id == doc_uuid))
            await db.execute(delete(Document).where(Document.id == doc_uuid))
            await db.commit()
        await clean_engine.dispose()

    asyncio.run(_cleanup())


# ---------------------------------------------------------------------------
# Tests — each asserts one concern against the shared pipeline_results
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestE2EPipeline:

    def test_upload_accepted(self, pipeline_results):
        assert pipeline_results["upload_status_code"] == 202, (
            f"Upload failed: {pipeline_results.get('upload_body')}"
        )
        data = pipeline_results["upload_body"]["data"]
        assert data["status"] == "queued"
        assert uuid.UUID(data["document_id"])  # valid UUID, no exception

    def test_job_lands_in_arq_queue(self, pipeline_results):
        """Upload enqueues to 'arq:queue' — the queue WorkerSettings.queue_name reads."""
        depth = pipeline_results["queue_depth_after_upload"]
        assert depth >= 1, (
            "No job found in arq:queue immediately after upload. "
            "Queue-name mismatch between API pool and WorkerSettings, "
            "or the arq pool was not initialised on app.state."
        )

    def test_worker_queue_name_matches_api_pool_default(self, pipeline_results):
        """WorkerSettings.queue_name must equal the ArqRedis pool default ('arq:queue').
        This exact mismatch was the Phase 2c C1 bug — it silently dropped every job
        for three phases because no test checked both sides of the wiring."""
        from omnivore.worker.main import WorkerSettings
        assert WorkerSettings.queue_name == "arq:queue", (
            f"WorkerSettings.queue_name={WorkerSettings.queue_name!r} "
            "does not match the API pool default 'arq:queue'."
        )

    def test_ingest_dispatch_returns_indexed(self, pipeline_results):
        result = pipeline_results["ingest_result"]
        assert result["status"] == "indexed", f"ingest_dispatch returned: {result}"
        assert result["chunks"] >= 1

    def test_document_status_is_indexed_in_db(self, pipeline_results):
        assert pipeline_results["status_code"] == 200
        doc = pipeline_results["doc_data"]
        assert doc["status"] == "indexed", f"Unexpected status: {doc['status']}"
        assert doc["indexed_at"] is not None

    def test_document_handler_recorded(self, pipeline_results):
        doc = pipeline_results["doc_data"]
        assert doc["handler"] is not None, "handler_name was not written to the Document row"

    def test_search_returns_results(self, pipeline_results):
        assert pipeline_results["search_status_code"] == 200
        results = pipeline_results["search_results"]
        assert len(results) >= 1, "BM25 search returned no results for the indexed document"

    def test_search_result_contains_uploaded_content(self, pipeline_results):
        results = pipeline_results["search_results"]
        all_content = " ".join(r["content"] for r in results).lower()
        assert any(
            keyword in all_content
            for keyword in ["pipeline", "queue", "ingestion", "worker", "arq"]
        ), f"None of the expected keywords found in search results: {all_content[:200]}"

    def test_search_result_schema(self, pipeline_results):
        for row in pipeline_results["search_results"]:
            assert "chunk_id" in row
            assert "document_id" in row
            assert "content" in row
            assert "score" in row
            assert row["score"] > 0

    # Phase 3 enrichment assertions ----------------------------------------

    def test_chunks_have_language_detected(self, pipeline_results):
        """Language detection must run for all chunks — English fixture → 'en'."""
        languages = pipeline_results.get("chunk_languages", [])
        assert len(languages) >= 1, "No chunks found — check ingest_dispatch ran"
        assert all(
            lang == "en" for lang in languages
        ), f"Expected all chunks to have language='en', got {languages}"

    def test_ner_produced_entities(self, pipeline_results):
        """spaCy NER must extract at least one entity from the English test fixture."""
        count = pipeline_results.get("entity_count", -1)
        assert count >= 1, (
            f"NER produced {count} entities — expected ≥1 from the English test document. "
            f"Labels found: {pipeline_results.get('entity_labels', [])}"
        )

    def test_routing_decision_populated(self, pipeline_results):
        """routing_decision must be persisted and have a non-empty sink_counts."""
        rd = pipeline_results.get("routing_decision")
        assert rd is not None, "routing_decision was not written to the Document row"
        assert rd.get("sink_counts"), f"routing_decision.sink_counts is empty: {rd}"
        assert rd.get("policy") == "default", f"Unexpected policy value: {rd.get('policy')}"
