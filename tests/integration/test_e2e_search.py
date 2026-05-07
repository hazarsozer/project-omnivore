"""End-to-end smoke test: insert indexed chunks → search with all 3 modes.

Uses testcontainers (Postgres 16 + Redis 7). No MinIO required. The embedding
model is not downloaded — embed_chunks is patched to return deterministic
unit vectors so the test runs in CI without GPU or network access.

Run:
    uv run pytest tests/integration/test_e2e_search.py -v

Skip in environments without Docker:
    uv run pytest -m "not integration"
"""
from __future__ import annotations

import asyncio
import math
import os
import subprocess
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from omnivore.constants import DEFAULT_TENANT_ID
from omnivore.db.models import Chunk as ChunkRow
from omnivore.db.models import Document
from omnivore.pipeline.embeddings import EMBEDDING_DIM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(seed: int) -> list[float]:
    """Deterministic L2-normalised vector — distinguishable per seed."""
    v = [math.sin(seed + i * 0.37) for i in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


# Three chunks with content that spans BM25 and vector retrieval well.
_CHUNKS = [
    ("Omnivore is a document ingestion pipeline designed for RAG systems.", 1),
    ("The API is built with FastAPI and uses ARQ for background job processing.", 2),
    ("pgvector stores dense embeddings directly inside PostgreSQL.", 3),
]


# ---------------------------------------------------------------------------
# Module-scoped container + migration fixture (runs once per test module)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_url():
    """Start Postgres 16, apply migrations, yield async URL."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        url = f"postgresql+asyncpg://test:test@{host}:{port}/test"

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Migration failed:\n{result.stderr}"
        yield url


@pytest.fixture(scope="module")
def redis_url():
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as rc:
        yield f"redis://localhost:{rc.get_exposed_port(6379)}"


# ---------------------------------------------------------------------------
# Seed: one document + three indexed chunks with embeddings
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded_doc_id(pg_url):
    """Insert a tenant, document, and 3 chunks with known embeddings. Returns doc_id."""
    doc_id = uuid.uuid4()

    async def _setup():
        engine = create_async_engine(pg_url)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            # Migration 0002 already seeds DEFAULT_TENANT_ID — insert Document only.
            db.add(Document(
                id=doc_id,
                tenant_id=DEFAULT_TENANT_ID,
                sha256=b"e" * 32,
                filename="sample.md",
                mime_type="text/markdown",
                size_bytes=256,
                storage_uri="s3://test/sample.md",
                status="indexed",
            ))
            # Flush parent rows first so FK constraints are satisfied.
            await db.flush()
            for ordinal, (content, seed) in enumerate(_CHUNKS):
                db.add(ChunkRow(
                    document_id=doc_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    ordinal=ordinal,
                    kind="text",
                    content=content,
                    token_count=len(content.split()),
                    position={"index": ordinal},
                    embedding=_unit_vec(seed),
                    embedding_model="test-model",
                ))
            await db.commit()
        await engine.dispose()

    asyncio.run(_setup())
    return doc_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSearchModes:
    """Each test gets a fresh async DB session against the seeded container."""

    @pytest_asyncio.fixture(autouse=True)
    async def db(self, pg_url):
        engine = create_async_engine(pg_url)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            self._db = session
            yield
        await engine.dispose()

    async def test_bm25_returns_matching_chunk(self, seeded_doc_id):
        from omnivore.api.routes.search import _bm25_search

        rows = await _bm25_search(self._db, "ingestion pipeline", DEFAULT_TENANT_ID, top_k=5)

        assert len(rows) >= 1
        assert any("ingestion" in r["content"].lower() for r in rows)

    async def test_vector_returns_nearest_chunk(self, seeded_doc_id):
        from omnivore.api.routes.search import _vector_search

        # Query with seed-3 vector → should rank the pgvector chunk highest
        rows = await _vector_search(self._db, _unit_vec(3), DEFAULT_TENANT_ID, top_k=5)

        assert len(rows) >= 1
        assert rows[0]["score"] > 0.9  # near-exact match (same unit vector)
        assert "pgvector" in rows[0]["content"].lower()

    async def test_hybrid_returns_results_for_all_chunks(self, seeded_doc_id):
        from omnivore.api.routes.search import _hybrid_search

        rows = await _hybrid_search(
            self._db, "PostgreSQL embeddings", _unit_vec(3), DEFAULT_TENANT_ID, top_k=10
        )

        assert len(rows) >= 1
        contents = {r["content"] for r in rows}
        # The pgvector chunk matches both BM25 ("PostgreSQL", "embeddings") and vector
        assert any("pgvector" in c.lower() for c in contents)

    async def test_hybrid_scores_are_positive_and_ordered(self, seeded_doc_id):
        from omnivore.api.routes.search import _hybrid_search

        rows = await _hybrid_search(
            self._db, "pipeline", _unit_vec(1), DEFAULT_TENANT_ID, top_k=10
        )

        assert len(rows) >= 1
        scores = [r["score"] for r in rows]
        assert all(s > 0 for s in scores)
        assert scores == sorted(scores, reverse=True)

    async def test_bm25_no_match_returns_empty(self, seeded_doc_id):
        from omnivore.api.routes.search import _bm25_search

        rows = await _bm25_search(
            self._db, "xyzzy quantum frobnicator", DEFAULT_TENANT_ID, top_k=5
        )
        assert rows == []

    async def test_result_schema_has_required_fields(self, seeded_doc_id):
        from omnivore.api.routes.search import _bm25_search

        rows = await _bm25_search(self._db, "ingestion", DEFAULT_TENANT_ID, top_k=5)
        assert len(rows) >= 1
        for row in rows:
            assert "chunk_id" in row
            assert "document_id" in row
            assert "content" in row
            assert "score" in row
            assert "token_count" in row
