"""Hybrid search endpoint — BM25 + vector (HNSW) + RRF merge."""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from omnivore.api.schemas import APIResponse
from omnivore.config import get_settings
from omnivore.db.session import AsyncSessionLocal
from omnivore.pipeline.embeddings import embed_texts

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/search", tags=["search"])

_HARDCODED_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RRF_K = 60


class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"  # hybrid | bm25 | vector
    top_k: int = 20


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    heading_path: list[str] | None
    score: float
    token_count: int


@router.post("")
async def search(request: Request, body: SearchRequest) -> APIResponse[list[SearchResult]]:
    settings = get_settings()
    tenant_id = _HARDCODED_TENANT

    # Embed query when vector retrieval is requested
    query_vector: list[float] | None = None
    if body.mode in ("hybrid", "vector"):
        redis = getattr(request.app.state, "arq_pool", None)
        vectors = await embed_texts([body.query], settings, redis)
        query_vector = vectors[0]

    # Degrade gracefully to BM25 when API key is absent
    effective_mode = body.mode
    if effective_mode in ("hybrid", "vector") and query_vector is None:
        effective_mode = "bm25"
        logger.warning("search.fallback_bm25", reason="embedding_unavailable")

    async with AsyncSessionLocal() as db:
        if effective_mode == "bm25":
            rows = await _bm25_search(db, body.query, tenant_id, body.top_k)
        elif effective_mode == "vector":
            rows = await _vector_search(db, query_vector, tenant_id, body.top_k)  # type: ignore[arg-type]
        else:
            rows = await _hybrid_search(db, body.query, query_vector, tenant_id, body.top_k)  # type: ignore[arg-type]

    results = [
        SearchResult(
            chunk_id=str(r["chunk_id"]),
            document_id=str(r["document_id"]),
            content=r["content"],
            heading_path=r["heading_path"],
            score=float(r["score"]),
            token_count=r["token_count"],
        )
        for r in rows
    ]

    logger.info("search.complete", query=body.query[:80], mode=effective_mode, results=len(results))
    return APIResponse(success=True, data=results)


async def _bm25_search(
    db: Any, query: str, tenant_id: uuid.UUID, top_k: int
) -> list[dict]:
    sql = text("""
        SELECT
            c.id            AS chunk_id,
            c.document_id,
            c.content,
            c.heading_path,
            c.token_count,
            ts_rank(c.content_tsv, q)::float AS score
        FROM core.chunks c,
             plainto_tsquery('simple', :query) AS q
        WHERE c.tenant_id = :tenant_id
          AND c.content_tsv @@ q
        ORDER BY score DESC
        LIMIT :top_k
    """)
    result = await db.execute(sql, {"query": query, "tenant_id": tenant_id, "top_k": top_k})
    return [dict(r._mapping) for r in result]


async def _vector_search(
    db: Any, vector: list[float], tenant_id: uuid.UUID, top_k: int
) -> list[dict]:
    vec_str = "[" + ",".join(str(x) for x in vector) + "]"
    sql = text("""
        SELECT
            c.id            AS chunk_id,
            c.document_id,
            c.content,
            c.heading_path,
            c.token_count,
            (1.0 - (c.embedding <=> CAST(:vec AS vector(1536))))::float AS score
        FROM core.chunks c
        WHERE c.tenant_id = :tenant_id
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:vec AS vector(1536))
        LIMIT :top_k
    """)
    result = await db.execute(sql, {"vec": vec_str, "tenant_id": tenant_id, "top_k": top_k})
    return [dict(r._mapping) for r in result]


async def _hybrid_search(
    db: Any, query: str, vector: list[float], tenant_id: uuid.UUID, top_k: int
) -> list[dict]:
    pre_k = min(top_k * 4, 200)
    vec_str = "[" + ",".join(str(x) for x in vector) + "]"
    sql = text("""
        WITH bm25 AS (
            SELECT c.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank(c.content_tsv, q) DESC) AS rank
            FROM core.chunks c,
                 plainto_tsquery('simple', :query) AS q
            WHERE c.tenant_id = :tenant_id
              AND c.content_tsv @@ q
            ORDER BY ts_rank(c.content_tsv, q) DESC
            LIMIT :pre_k
        ),
        vec AS (
            SELECT id,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:vec AS vector(1536))) AS rank
            FROM core.chunks
            WHERE tenant_id = :tenant_id
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:vec AS vector(1536))
            LIMIT :pre_k
        ),
        rrf AS (
            SELECT
                COALESCE(b.id, v.id) AS id,
                COALESCE(1.0 / (:rrf_k + b.rank), 0.0) +
                COALESCE(1.0 / (:rrf_k + v.rank), 0.0) AS rrf_score
            FROM bm25 b
            FULL OUTER JOIN vec v ON b.id = v.id
        )
        SELECT
            c.id            AS chunk_id,
            c.document_id,
            c.content,
            c.heading_path,
            c.token_count,
            r.rrf_score::float AS score
        FROM rrf r
        JOIN core.chunks c ON c.id = r.id
        ORDER BY r.rrf_score DESC
        LIMIT :top_k
    """)
    result = await db.execute(sql, {
        "query": query,
        "vec": vec_str,
        "tenant_id": tenant_id,
        "top_k": top_k,
        "pre_k": pre_k,
        "rrf_k": _RRF_K,
    })
    return [dict(r._mapping) for r in result]
