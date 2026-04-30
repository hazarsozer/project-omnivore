from __future__ import annotations

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


class StubMetricsEvaluator:
    async def extraction_accuracy(self, result: dict, expected: dict) -> float | None:
        return None  # implemented in Phase 3

    async def chunk_faithfulness(self, chunks: list[dict], source_content: str) -> float | None:
        return None  # implemented in Phase 3

    async def retrieval_recall_at_k(
        self, query_results: list[dict], relevant_ids: set[str], k: int
    ) -> float | None:
        return None  # implemented in Phase 3

    async def ndcg_at_k(
        self, query_results: list[dict], relevance_scores: dict[str, int], k: int
    ) -> float | None:
        return None  # implemented in Phase 3
