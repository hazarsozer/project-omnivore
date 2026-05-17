"""Hybrid search endpoint — BM25 + vector (HNSW) + RRF merge."""
from __future__ import annotations

import hashlib
import time as _time
import uuid
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from omnivore.api.schemas import APIResponse
from omnivore.auth.context import AuthContext
from omnivore.auth.dependencies import rate_limited, require_scope
from omnivore.db.session import tenant_session
from omnivore.observability import SEARCH_DURATION
from omnivore.pipeline.embeddings import QUERY_PREFIX, embed_texts

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/search", tags=["search"])
_RRF_K = 60


class SearchRequest(BaseModel):
    query: str
    mode: Literal["bm25", "vector", "hybrid"] = "hybrid"
    top_k: int = Field(20, ge=1, le=200)


class SearchResult(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    heading_path: list[str] | None
    score: float
    token_count: int


@router.post("")
async def search(
    request: Request,
    body: SearchRequest,
    auth: Annotated[AuthContext, Depends(require_scope("search:read"))],
    _rl: Annotated[None, Depends(rate_limited())] = None,
) -> APIResponse[list[SearchResult]]:
    tenant_id = auth.tenant_id
    _t0 = _time.perf_counter()

    query_vector: list[float] | None = None
    if body.mode in ("hybrid", "vector"):
        redis = getattr(request.app.state, "arq_pool", None)
        query_vector = (await embed_texts([body.query], redis, query_prefix=QUERY_PREFIX))[0]

    async with tenant_session(tenant_id) as db:
        if body.mode == "bm25":
            rows = await _bm25_search(db, body.query, tenant_id, body.top_k)
        elif body.mode == "vector":
            rows = await _vector_search(db, query_vector, tenant_id, body.top_k)  # type: ignore[arg-type]
        else:
            rows = await _hybrid_search(db, body.query, query_vector, tenant_id, body.top_k)  # type: ignore[arg-type]

    SEARCH_DURATION.labels(mode=body.mode).observe(_time.perf_counter() - _t0)
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

    query_hash = hashlib.sha256(body.query.encode()).hexdigest()[:12]
    logger.info("search.complete", query_hash=query_hash, mode=body.mode, results=len(results))
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
            (1.0 - (c.embedding <=> CAST(:vec AS vector(768))))::float AS score
        FROM core.chunks c
        WHERE c.tenant_id = :tenant_id
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:vec AS vector(768))
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
                   ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:vec AS vector(768))) AS rank
            FROM core.chunks
            WHERE tenant_id = :tenant_id
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:vec AS vector(768))
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
        ORDER BY r.rrf_score DESC, c.id
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
