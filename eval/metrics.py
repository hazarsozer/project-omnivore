from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class EvalResult:
    fixture_id: str
    handler: str
    handler_version: str
    run_at: datetime
    metrics: dict[str, float | None]
    errors: list[str] = field(default_factory=list)


class MetricsEvaluator(Protocol):
    async def extraction_accuracy(self, result: dict, expected: dict) -> float | None: ...
    async def chunk_faithfulness(self, chunks: list[dict], source_content: str) -> float | None: ...
    async def retrieval_recall_at_k(
        self, query_results: list[dict], relevant_ids: set[str], k: int
    ) -> float | None: ...
    async def ndcg_at_k(
        self, query_results: list[dict], relevance_scores: dict[str, int], k: int
    ) -> float | None: ...


def compute_chunk_faithfulness(chunks: list, queries: list[dict]) -> float | None:
    """Substring-based faithfulness metric (offline, no retrieval needed).

    For each query with relevant_content_substrings, check whether at least one
    substring appears (case-insensitive) in any chunk's content. Returns the
    fraction of queries satisfied, or None if no queries have substrings defined.
    """
    scored_queries = [q for q in queries if q.get("relevant_content_substrings")]
    if not scored_queries:
        return None

    all_content_lower = " ".join(
        getattr(c, "content", c.get("content", "") if isinstance(c, dict) else "")
        for c in chunks
    ).lower()

    hits = 0
    for q in scored_queries:
        substrings = q["relevant_content_substrings"]
        if any(s.lower() in all_content_lower for s in substrings):
            hits += 1

    return round(hits / len(scored_queries), 4)


class StubMetricsEvaluator:
    async def extraction_accuracy(self, result: dict, expected: dict) -> float | None:
        return None

    async def chunk_faithfulness(self, chunks: list[dict], source_content: str) -> float | None:
        return None

    async def retrieval_recall_at_k(
        self, query_results: list[dict], relevant_ids: set[str], k: int
    ) -> float | None:
        return None

    async def ndcg_at_k(
        self, query_results: list[dict], relevance_scores: dict[str, int], k: int
    ) -> float | None:
        return None


class LiveMetricsEvaluator:
    """Real recall@k and nDCG@k metrics for the Phase 2b embedding bakeoff.

    query_results: list of {"chunk_id": str, "score": float} ranked best-first.
    relevant_ids: set of chunk IDs considered ground-truth relevant.
    relevance_scores: dict {chunk_id: int} where higher = more relevant (binary: 0 or 1).
    """

    async def extraction_accuracy(self, result: dict, expected: dict) -> float | None:
        return None  # Phase 3

    async def chunk_faithfulness(self, chunks: list[dict], source_content: str) -> float | None:
        return None  # Phase 3

    async def retrieval_recall_at_k(
        self, query_results: list[dict], relevant_ids: set[str], k: int
    ) -> float | None:
        if not relevant_ids:
            return None
        retrieved = {r["chunk_id"] for r in query_results[:k]}
        return round(len(retrieved & relevant_ids) / len(relevant_ids), 4)

    async def ndcg_at_k(
        self, query_results: list[dict], relevance_scores: dict[str, int], k: int
    ) -> float | None:
        if not relevance_scores:
            return None

        dcg = 0.0
        for rank, result in enumerate(query_results[:k], start=1):
            rel = relevance_scores.get(result["chunk_id"], 0)
            dcg += rel / math.log2(rank + 1)

        ideal_rels = sorted(relevance_scores.values(), reverse=True)[:k]
        idcg = sum(rel / math.log2(rank + 1) for rank, rel in enumerate(ideal_rels, start=1))

        if idcg == 0.0:
            return 0.0
        return round(dcg / idcg, 4)
