"""Unit tests for eval/metrics.py LiveMetricsEvaluator and eval/bakeoff helpers."""
from __future__ import annotations

import uuid

import numpy as np
import pytest

from eval.bakeoff import _rank_chunks, _relevant_ids_for_query
from eval.metrics import LiveMetricsEvaluator
from omnivore.pipeline.models import Chunk

# ---------------------------------------------------------------------------
# LiveMetricsEvaluator — recall@k
# ---------------------------------------------------------------------------

evaluator = LiveMetricsEvaluator()


async def test_recall_at_k_perfect():
    relevant = {"a", "b", "c"}
    results = [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}, {"chunk_id": "d"}]
    r = await evaluator.retrieval_recall_at_k(results, relevant, k=3)
    assert r == 1.0


async def test_recall_at_k_partial():
    relevant = {"a", "b", "c"}
    results = [{"chunk_id": "a"}, {"chunk_id": "x"}, {"chunk_id": "x2"}]
    r = await evaluator.retrieval_recall_at_k(results, relevant, k=3)
    assert r == pytest.approx(1 / 3, abs=1e-4)


async def test_recall_at_k_zero():
    relevant = {"a", "b"}
    results = [{"chunk_id": "x"}, {"chunk_id": "y"}]
    r = await evaluator.retrieval_recall_at_k(results, relevant, k=10)
    assert r == 0.0


async def test_recall_at_k_none_when_no_relevant():
    r = await evaluator.retrieval_recall_at_k([{"chunk_id": "x"}], set(), k=10)
    assert r is None


async def test_recall_respects_k_cutoff():
    relevant = {"a", "b"}
    # 'a' is at rank 2 — within k=2, 'b' is at rank 3 — outside k=2
    results = [{"chunk_id": "x"}, {"chunk_id": "a"}, {"chunk_id": "b"}]
    r = await evaluator.retrieval_recall_at_k(results, relevant, k=2)
    assert r == pytest.approx(0.5, abs=1e-4)


# ---------------------------------------------------------------------------
# LiveMetricsEvaluator — nDCG@k
# ---------------------------------------------------------------------------

async def test_ndcg_perfect_ranking():
    relevant_scores = {"a": 1, "b": 1, "c": 0}
    results = [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}]
    ndcg = await evaluator.ndcg_at_k(results, relevant_scores, k=3)
    assert ndcg == pytest.approx(1.0, abs=1e-4)


async def test_ndcg_worst_ranking():
    relevant_scores = {"a": 1, "b": 0, "c": 0}
    # relevant item at last position
    results = [{"chunk_id": "b"}, {"chunk_id": "c"}, {"chunk_id": "a"}]
    ndcg = await evaluator.ndcg_at_k(results, relevant_scores, k=3)
    # DCG = 1/log2(4), IDCG = 1/log2(2)
    import math
    expected = (1 / math.log2(4)) / (1 / math.log2(2))
    assert ndcg == pytest.approx(expected, abs=1e-4)


async def test_ndcg_none_when_no_relevant():
    ndcg = await evaluator.ndcg_at_k([{"chunk_id": "x"}], {}, k=10)
    assert ndcg is None


async def test_ndcg_zero_when_all_irrelevant_retrieved():
    relevant_scores = {"a": 1}
    results = [{"chunk_id": "x"}, {"chunk_id": "y"}]
    ndcg = await evaluator.ndcg_at_k(results, relevant_scores, k=2)
    assert ndcg == 0.0


# ---------------------------------------------------------------------------
# _rank_chunks
# ---------------------------------------------------------------------------

def test_rank_chunks_highest_score_first():
    query_vec = np.array([1.0, 0.0], dtype=np.float32)
    doc_vecs = np.array([
        [0.5, 0.866],  # cos ~ 0.5
        [0.866, 0.5],  # cos ~ 0.866
        [0.0, 1.0],    # cos = 0.0
    ], dtype=np.float32)
    ids = ["a", "b", "c"]

    ranked = _rank_chunks(query_vec, doc_vecs, ids)

    assert ranked[0]["chunk_id"] == "b"
    assert ranked[-1]["chunk_id"] == "c"
    assert ranked[0]["score"] > ranked[1]["score"] > ranked[2]["score"]


def test_rank_chunks_returns_all_ids():
    query_vec = np.array([1.0, 0.0], dtype=np.float32)
    doc_vecs = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    ranked = _rank_chunks(query_vec, doc_vecs, ["x", "y"])
    assert {r["chunk_id"] for r in ranked} == {"x", "y"}


# ---------------------------------------------------------------------------
# _relevant_ids_for_query
# ---------------------------------------------------------------------------

def _make_chunk(content: str, idx: int = 0) -> Chunk:
    return Chunk(
        document_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        ordinal=idx,
        kind="text",
        content=content,
        token_count=len(content.split()),
        position={},
        heading_path=[],
        source_block_ids=[],
    )


def test_relevant_ids_matches_substring():
    chunks = [
        _make_chunk("PostgreSQL stores all data"),
        _make_chunk("FastAPI handles the REST layer"),
        _make_chunk("Redis enables async queues"),
    ]
    ids = ["0", "1", "2"]
    relevant = _relevant_ids_for_query(chunks, ids, ["PostgreSQL"])
    assert relevant == {"0"}


def test_relevant_ids_multiple_substrings():
    chunks = [
        _make_chunk("PostgreSQL and pgvector for embeddings"),
        _make_chunk("FastAPI REST layer"),
    ]
    ids = ["0", "1"]
    relevant = _relevant_ids_for_query(chunks, ids, ["PostgreSQL", "pgvector"])
    assert "0" in relevant


def test_relevant_ids_case_insensitive():
    chunks = [_make_chunk("POSTGRESQL stores data")]
    relevant = _relevant_ids_for_query(chunks, ["id-0"], ["postgresql"])
    assert relevant == {"id-0"}


def test_relevant_ids_empty_when_no_match():
    chunks = [_make_chunk("Completely unrelated content")]
    relevant = _relevant_ids_for_query(chunks, ["0"], ["PostgreSQL"])
    assert relevant == set()


def test_relevant_ids_multiple_chunks_matched():
    chunks = [
        _make_chunk("PostgreSQL is the primary DB"),
        _make_chunk("We use PostgreSQL for storage"),
        _make_chunk("Redis handles queues"),
    ]
    ids = ["fixture:0", "fixture:1", "fixture:2"]
    relevant = _relevant_ids_for_query(chunks, ids, ["PostgreSQL"])
    assert relevant == {"fixture:0", "fixture:1"}
