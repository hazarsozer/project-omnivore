"""Phase 2b embedding bakeoff.

Runs three candidate embedding models against the fixture query gold-set
and records recall@10 and nDCG@10 for each. Purely in-memory: no Postgres
is needed — this measures vector-search quality in isolation.

Usage:
    uv run python -m eval.bakeoff
    uv run python -m eval.bakeoff --candidates bge-base nomic
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import structlog

from eval.metrics import LiveMetricsEvaluator
from eval.runner import _DiskCtx
from omnivore.pipeline.chunker import chunk_result
from omnivore.pipeline.context import BlobRef
from omnivore.pipeline.registry import HandlerRegistry

logger = structlog.get_logger(__name__)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_K = 10


@dataclass
class EmbedCandidate:
    key: str
    model_id: str
    dim: int
    query_prefix: str
    trust_remote_code: bool = False


CANDIDATES: list[EmbedCandidate] = [
    EmbedCandidate(
        key="bge-base",
        model_id="BAAI/bge-base-en-v1.5",
        dim=768,
        query_prefix="Represent this sentence for searching relevant passages: ",
    ),
    EmbedCandidate(
        key="bge-m3",
        model_id="BAAI/bge-m3",
        dim=1024,
        query_prefix="",
    ),
    EmbedCandidate(
        key="nomic",
        model_id="nomic-ai/nomic-embed-text-v1.5",
        dim=768,
        query_prefix="search_query: ",
        trust_remote_code=True,
    ),
]


@dataclass
class QueryScore:
    fixture_id: str
    query: str
    recall_at_10: float | None
    ndcg_at_10: float | None


@dataclass
class CandidateResult:
    candidate_key: str
    model_id: str
    query_scores: list[QueryScore] = field(default_factory=list)
    mean_recall_at_10: float | None = None
    mean_ndcg_at_10: float | None = None
    error: str | None = None


def _load_model(candidate: EmbedCandidate):
    from sentence_transformers import SentenceTransformer

    kwargs: dict = {}
    if candidate.trust_remote_code:
        kwargs["trust_remote_code"] = True

    model = SentenceTransformer(candidate.model_id, device="cpu", **kwargs)
    logger.info("bakeoff.model_loaded", model=candidate.model_id)
    return model


def _encode(model, texts: list[str], prefix: str = "") -> np.ndarray:
    effective = [prefix + t for t in texts] if prefix else list(texts)
    vecs = model.encode(
        effective,
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return np.array(vecs, dtype=np.float32)


def _rank_chunks(
    query_vec: np.ndarray,
    chunk_vecs: np.ndarray,
    chunk_ids: list[str],
) -> list[dict]:
    # Both L2-normalized → cosine = dot product
    scores = (chunk_vecs @ query_vec).tolist()
    ranked = sorted(zip(chunk_ids, scores), key=lambda x: x[1], reverse=True)
    return [{"chunk_id": cid, "score": s} for cid, s in ranked]


def _relevant_ids_for_query(chunks: list, substrings: list[str]) -> set[str]:
    """Return chunk IDs whose content contains any of the relevance substrings."""
    relevant: set[str] = set()
    for i, chunk in enumerate(chunks):
        if any(sub.lower() in chunk.content.lower() for sub in substrings):
            relevant.add(str(i))  # use ordinal as stable ID within this run
    return relevant


async def _run_fixture(
    fixture_dir: Path,
    meta: dict,
    registry: HandlerRegistry,
    model,
    candidate: EmbedCandidate,
    evaluator: LiveMetricsEvaluator,
) -> list[QueryScore]:
    source_files = [p for p in fixture_dir.iterdir() if p.name != "meta.json"]
    if not source_files:
        return []

    source_file = source_files[0]
    data = source_file.read_bytes()

    import magic

    detected_mime = meta.get("expected_mime") or magic.from_buffer(data[:2048], mime=True)
    blob = BlobRef(bucket="eval", key=source_file.name, mime_type=detected_mime, size_bytes=len(data))
    ctx = _DiskCtx(path=source_file, filename=source_file.name, config={})

    handler_cls = registry.resolve(detected_mime)
    if handler_cls is None:
        logger.warning("bakeoff.no_handler", fixture=meta["fixture_id"], mime=detected_mime)
        return []

    handler = handler_cls()
    result = await handler.extract(blob, ctx)
    chunks = chunk_result(result, _TENANT)
    if not chunks:
        return []

    chunk_texts = [c.content for c in chunks]
    chunk_ids = [str(i) for i in range(len(chunks))]

    loop = asyncio.get_running_loop()
    chunk_vecs = await loop.run_in_executor(None, _encode, model, chunk_texts, "")

    query_scores: list[QueryScore] = []
    for q in meta.get("queries", []):
        query_text = q["query"]
        substrings = q.get("relevant_content_substrings", [])
        relevant = _relevant_ids_for_query(chunks, substrings)

        if not relevant:
            logger.warning(
                "bakeoff.no_relevant_chunks",
                fixture=meta["fixture_id"],
                query=query_text[:50],
            )
            continue

        query_vec = await loop.run_in_executor(
            None, _encode, model, [query_text], candidate.query_prefix
        )
        ranked = _rank_chunks(query_vec[0], chunk_vecs, chunk_ids)

        recall = await evaluator.retrieval_recall_at_k(ranked, relevant, k=_K)
        relevance_scores = {cid: (1 if cid in relevant else 0) for cid in chunk_ids}
        ndcg = await evaluator.ndcg_at_k(ranked, relevance_scores, k=_K)

        query_scores.append(
            QueryScore(
                fixture_id=meta["fixture_id"],
                query=query_text,
                recall_at_10=recall,
                ndcg_at_10=ndcg,
            )
        )

    return query_scores


async def run_bakeoff(
    fixtures_dir: Path,
    results_dir: Path,
    candidate_keys: list[str] | None = None,
) -> list[CandidateResult]:
    results_dir.mkdir(parents=True, exist_ok=True)

    # Load fixture catalog — only fixtures that have queries
    catalog = []
    for meta_path in sorted(fixtures_dir.rglob("meta.json")):
        with meta_path.open() as f:
            meta = json.load(f)
        if not meta.get("queries"):
            continue
        meta["_fixture_dir"] = str(meta_path.parent)
        catalog.append(meta)

    if not catalog:
        print("No fixtures with queries found. Add 'queries' to meta.json files.")
        return []

    registry = HandlerRegistry()
    registry.discover()

    candidates = CANDIDATES
    if candidate_keys:
        candidates = [c for c in CANDIDATES if c.key in candidate_keys]

    evaluator = LiveMetricsEvaluator()
    all_results: list[CandidateResult] = []

    for candidate in candidates:
        print(f"\n{'='*60}")
        print(f"Model: {candidate.model_id}")
        print(f"{'='*60}")

        candidate_result = CandidateResult(
            candidate_key=candidate.key,
            model_id=candidate.model_id,
        )

        try:
            loop = asyncio.get_running_loop()
            model = await loop.run_in_executor(None, _load_model, candidate)

            for meta in catalog:
                fixture_id = meta["fixture_id"]
                fixture_dir = Path(meta["_fixture_dir"])
                print(f"  {fixture_id}:")

                scores = await _run_fixture(
                    fixture_dir, meta, registry, model, candidate, evaluator
                )
                candidate_result.query_scores.extend(scores)

                for qs in scores:
                    print(
                        f"    [{qs.fixture_id}] {qs.query[:50]!r}"
                        f"  recall@10={qs.recall_at_10}  nDCG@10={qs.ndcg_at_10}"
                    )

            recalls = [qs.recall_at_10 for qs in candidate_result.query_scores if qs.recall_at_10 is not None]
            ndcgs = [qs.ndcg_at_10 for qs in candidate_result.query_scores if qs.ndcg_at_10 is not None]

            if recalls:
                candidate_result.mean_recall_at_10 = round(sum(recalls) / len(recalls), 4)
            if ndcgs:
                candidate_result.mean_ndcg_at_10 = round(sum(ndcgs) / len(ndcgs), 4)

            print(
                f"\n  → mean recall@10={candidate_result.mean_recall_at_10}"
                f"  mean nDCG@10={candidate_result.mean_ndcg_at_10}"
            )

        except Exception as exc:
            logger.exception("bakeoff.candidate_failed", candidate=candidate.key)
            candidate_result.error = str(exc)
            print(f"  ERROR: {exc}")

        all_results.append(candidate_result)

    # Write baseline JSON
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / "phase2b-bakeoff-baseline.json"
    payload = {
        "run_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "k": _K,
        "reference_model": "bge-base",
        "candidates": [
            {
                "key": r.candidate_key,
                "model_id": r.model_id,
                "mean_recall_at_10": r.mean_recall_at_10,
                "mean_ndcg_at_10": r.mean_ndcg_at_10,
                "error": r.error,
                "query_scores": [
                    {
                        "fixture_id": qs.fixture_id,
                        "query": qs.query,
                        "recall_at_10": qs.recall_at_10,
                        "ndcg_at_10": qs.ndcg_at_10,
                    }
                    for qs in r.query_scores
                ],
            }
            for r in all_results
        ],
    }
    with out_path.open("w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nBaseline written to {out_path}")
    return all_results


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2b embedding bakeoff")
    parser.add_argument(
        "--candidates",
        nargs="*",
        choices=["bge-base", "bge-m3", "nomic"],
        default=None,
        help="Candidate keys to run (default: all)",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("eval/fixtures"),
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("eval/results"),
    )
    args = parser.parse_args()
    asyncio.run(run_bakeoff(args.fixtures_dir, args.results_dir, args.candidates))


if __name__ == "__main__":
    main()
