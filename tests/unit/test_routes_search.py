"""Unit tests for api/routes/search.py — mocks DB and embed_texts."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from pydantic import ValidationError

from omnivore.api.routes.search import SearchRequest, search
from omnivore.auth.context import AuthContext
from omnivore.pipeline.embeddings import EMBEDDING_DIM

_TEST_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_VEC = [float(i % 100) / 100.0 for i in range(EMBEDDING_DIM)]


def _make_auth() -> AuthContext:
    return AuthContext(
        tenant_id=_TEST_TENANT_ID,
        principal_id="test-key-id",
        principal_type="api_key",
        scopes=frozenset(["search:read"]),
        raw_token_hash="",
    )


class _Row:
    def __init__(self, d: dict) -> None:
        self._mapping = d


def _sample_rows(n: int = 2) -> list[_Row]:
    return [
        _Row({
            "chunk_id": uuid.uuid4(),
            "document_id": uuid.uuid4(),
            "content": f"content {i}",
            "heading_path": ["Section"],
            "score": 1.0 / (i + 1),
            "token_count": 50,
        })
        for i in range(n)
    ]


def _make_db(rows: list[_Row]) -> AsyncMock:
    result = MagicMock()
    result.__iter__ = MagicMock(return_value=iter(rows))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _make_session_local(db: AsyncMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _make_request(arq_pool=None) -> MagicMock:
    req = MagicMock(spec=Request)
    req.app = MagicMock()
    req.app.state = MagicMock()
    req.app.state.arq_pool = arq_pool
    return req


# ---------------------------------------------------------------------------
# BM25 mode
# ---------------------------------------------------------------------------

async def test_search_bm25_returns_results():
    rows = _sample_rows(2)
    db = _make_db(rows)

    with patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)):
        resp = await search(
            request=_make_request(),
            body=SearchRequest(query="hello", mode="bm25"),
            auth=_make_auth(),
        )

    assert resp.success is True
    assert len(resp.data) == 2
    assert resp.data[0].content == "content 0"


async def test_search_bm25_empty_results():
    db = _make_db([])

    with patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)):
        resp = await search(
            request=_make_request(),
            body=SearchRequest(query="zzz", mode="bm25"),
            auth=_make_auth(),
        )

    assert resp.success is True
    assert resp.data == []


async def test_search_bm25_does_not_call_embed():
    db = _make_db(_sample_rows(1))

    with (
        patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)),
        patch("omnivore.api.routes.search.embed_texts", AsyncMock()) as mock_embed,
    ):
        await search(
            request=_make_request(),
            body=SearchRequest(query="q", mode="bm25"),
            auth=_make_auth(),
        )

    mock_embed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Vector mode
# ---------------------------------------------------------------------------

async def test_search_vector_embeds_query():
    rows = _sample_rows(1)
    db = _make_db(rows)

    with (
        patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)),
        patch("omnivore.api.routes.search.embed_texts", AsyncMock(return_value=[_VEC])) as mock_embed,
    ):
        resp = await search(
            request=_make_request(arq_pool=AsyncMock()),
            body=SearchRequest(query="neural search", mode="vector"),
            auth=_make_auth(),
        )

    mock_embed.assert_awaited_once()
    assert resp.success is True
    assert len(resp.data) == 1


async def test_search_vector_no_redis_still_embeds():
    """arq_pool=None is passed through to embed_texts; it handles caching internally."""
    db = _make_db(_sample_rows(1))

    with (
        patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)),
        patch("omnivore.api.routes.search.embed_texts", AsyncMock(return_value=[_VEC])) as mock_embed,
    ):
        resp = await search(
            request=_make_request(arq_pool=None),
            body=SearchRequest(query="dense", mode="vector"),
            auth=_make_auth(),
        )

    mock_embed.assert_awaited_once()
    assert resp.success is True


# ---------------------------------------------------------------------------
# Hybrid mode
# ---------------------------------------------------------------------------

async def test_search_hybrid_returns_results():
    db = _make_db(_sample_rows(3))

    with (
        patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)),
        patch("omnivore.api.routes.search.embed_texts", AsyncMock(return_value=[_VEC])),
    ):
        resp = await search(
            request=_make_request(arq_pool=AsyncMock()),
            body=SearchRequest(query="hybrid query", mode="hybrid"),
            auth=_make_auth(),
        )

    assert resp.success is True
    assert len(resp.data) == 3


async def test_search_hybrid_is_default_mode():
    db = _make_db(_sample_rows(1))

    with (
        patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)),
        patch("omnivore.api.routes.search.embed_texts", AsyncMock(return_value=[_VEC])),
    ):
        resp = await search(
            request=_make_request(arq_pool=AsyncMock()),
            body=SearchRequest(query="test"),
            auth=_make_auth(),
        )

    assert resp.success is True
    assert resp.data[0].content == "content 0"


# ---------------------------------------------------------------------------
# top_k validation
# ---------------------------------------------------------------------------

def test_search_request_top_k_below_min_fails():
    with pytest.raises(ValidationError):
        SearchRequest(query="x", top_k=0)


def test_search_request_top_k_above_max_fails():
    with pytest.raises(ValidationError):
        SearchRequest(query="x", top_k=201)


def test_search_request_top_k_at_boundaries_valid():
    assert SearchRequest(query="x", top_k=1).top_k == 1
    assert SearchRequest(query="x", top_k=200).top_k == 200


def test_search_request_default_top_k():
    body = SearchRequest(query="x")
    assert body.top_k == 20


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

async def test_search_result_fields_populated():
    chunk_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    rows = [_Row({
        "chunk_id": chunk_id,
        "document_id": doc_id,
        "content": "test content",
        "heading_path": ["H1", "H2"],
        "score": 0.75,
        "token_count": 42,
    })]
    db = _make_db(rows)

    with patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)):
        resp = await search(
            request=_make_request(),
            body=SearchRequest(query="q", mode="bm25"),
            auth=_make_auth(),
        )

    r = resp.data[0]
    assert r.chunk_id == str(chunk_id)
    assert r.document_id == str(doc_id)
    assert r.content == "test content"
    assert r.heading_path == ["H1", "H2"]
    assert r.score == pytest.approx(0.75)
    assert r.token_count == 42


async def test_search_result_null_heading_path():
    rows = [_Row({
        "chunk_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "content": "no headings",
        "heading_path": None,
        "score": 0.5,
        "token_count": 10,
    })]
    db = _make_db(rows)

    with patch("omnivore.api.routes.search.tenant_session", _make_session_local(db)):
        resp = await search(
            request=_make_request(),
            body=SearchRequest(query="q", mode="bm25"),
            auth=_make_auth(),
        )

    assert resp.data[0].heading_path is None


# ---------------------------------------------------------------------------
# Sinks filter — SQL contains the required 'relational'/'vector' guards
# ---------------------------------------------------------------------------

from omnivore.api.routes.search import _bm25_search, _hybrid_search, _vector_search  # noqa: E402


def _get_sql_string(db: AsyncMock) -> str:
    """Extract the SQL string that was passed to db.execute()."""
    call_args = db.execute.call_args
    sql_arg = call_args[0][0]  # first positional arg to execute()
    return str(sql_arg)


async def test_bm25_search_sql_contains_relational_sinks_filter():
    """_bm25_search must filter chunks WHERE 'relational' = ANY(c.sinks)."""
    db = _make_db([])

    await _bm25_search(db, "query", _TEST_TENANT_ID, 10)

    sql = _get_sql_string(db)
    assert "'relational' = ANY(c.sinks)" in sql, f"Missing sinks filter in BM25 SQL: {sql}"


async def test_vector_search_sql_contains_vector_sinks_filter():
    """_vector_search must filter chunks WHERE 'vector' = ANY(c.sinks)."""
    db = _make_db([])

    await _vector_search(db, _VEC, _TEST_TENANT_ID, 10)

    sql = _get_sql_string(db)
    assert "'vector' = ANY(c.sinks)" in sql, f"Missing sinks filter in vector SQL: {sql}"


async def test_hybrid_search_sql_contains_both_sinks_filters():
    """_hybrid_search CTEs must filter: bm25 on relational, vec on vector."""
    db = _make_db([])

    await _hybrid_search(db, "query", _VEC, _TEST_TENANT_ID, 10)

    sql = _get_sql_string(db)
    assert "'relational' = ANY(c.sinks)" in sql, f"Missing relational sinks filter in hybrid SQL: {sql}"
    assert "'vector' = ANY(sinks)" in sql, f"Missing vector sinks filter in hybrid SQL: {sql}"
