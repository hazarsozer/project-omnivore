"""Phase 2b embedding bakeoff.

Runs three candidate embedding models against the fixture query gold-set and records
recall@k and nDCG@k for k in {1, 3, 5, 10}.

KEY DESIGN: All fixture chunks are pooled into a single corpus so that ranking pressure
exists at any k. With 9 total chunks across three fixtures, k=1 requires the model to
rank the correct chunk first. Querying only within a fixture's own chunks (the previous
design) made recall@10 trivially 1.0 for any model.

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
    recall_at_1: float | None
    recall_at_3: float | None
    recall_at_5: float | None
    recall_at_10: float | None
    ndcg_at_1: float | None
    ndcg_at_3: float | None
    ndcg_at_5: float | None
    ndcg_at_10: float | None


@dataclass
class CandidateResult:
    candidate_key: str
    model_id: str
    corpus_size: int = 0
    query_scores: list[QueryScore] = field(default_factory=list)
    mean_recall_at_1: float | None = None
    mean_recall_at_3: float | None = None
    mean_ndcg_at_1: float | None = None
    mean_ndcg_at_3: float | None = None
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
    # Both L2-normalized; cosine similarity = dot product
    scores = (chunk_vecs @ query_vec).tolist()
    ranked = sorted(zip(chunk_ids, scores), key=lambda x: x[1], reverse=True)
    return [{"chunk_id": cid, "score": s} for cid, s in ranked]


def _relevant_ids_for_query(chunks: list, chunk_ids: list[str], substrings: list[str]) -> set[str]:
    """Return IDs of chunks in the COMBINED corpus whose content contains any relevance substring."""
    relevant: set[str] = set()
    for cid, chunk in zip(chunk_ids, chunks):
        if any(sub.lower() in chunk.content.lower() for sub in substrings):
            relevant.add(cid)
    return relevant


async def _build_corpus(
    catalog: list[dict],
    registry: HandlerRegistry,
) -> tuple[list, list[str]]:
    """Extract all chunks from all fixtures into one pooled corpus.

    chunk_id format: "<fixture_id>:<global_ordinal>" — stable within a run.
    Pooling makes k=1 discriminative even with small fixture sets.
    """
    import magic

    all_chunks: list = []
    all_chunk_ids: list[str] = []

    for meta in catalog:
        fixture_dir = Path(meta["_fixture_dir"])
        source_files = [p for p in fixture_dir.iterdir() if p.name != "meta.json"]
        if not source_files:
            continue

        source_file = source_files[0]
        data = source_file.read_bytes()
        detected_mime = meta.get("expected_mime") or magic.from_buffer(data[:2048], mime=True)
        blob = BlobRef(bucket="eval", key=source_file.name, mime_type=detected_mime, size_bytes=len(data))
        ctx = _DiskCtx(path=source_file, filename=source_file.name, config={})

        handler_cls = registry.resolve(detected_mime)
        if handler_cls is None:
            logger.warning("bakeoff.no_handler", fixture=meta["fixture_id"], mime=detected_mime)
            continue

        result = await handler_cls().extract(blob, ctx)
        chunks = chunk_result(result, _TENANT)

        for chunk in chunks:
            global_ordinal = len(all_chunks)
            all_chunk_ids.append(f"{meta['fixture_id']}:{global_ordinal}")
            all_chunks.append(chunk)

    return all_chunks, all_chunk_ids


async def _score_candidate(
    catalog: list[dict],
    corpus_chunks: list,
    corpus_ids: list[str],
    corpus_vecs: np.ndarray,
    model,
    candidate: EmbedCandidate,
    evaluator: LiveMetricsEvaluator,
) -> list[QueryScore]:
    loop = asyncio.get_running_loop()
    scores: list[QueryScore] = []

    for meta in catalog:
        for q in meta.get("queries", []):
            query_text = q["query"]
            substrings = q.get("relevant_content_substrings", [])
            relevant = _relevant_ids_for_query(corpus_chunks, corpus_ids, substrings)

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
            ranked = _rank_chunks(query_vec[0], corpus_vecs, corpus_ids)
            relevance_scores = {cid: (1 if cid in relevant else 0) for cid in corpus_ids}

            qs = QueryScore(
                fixture_id=meta["fixture_id"],
                query=query_text,
                recall_at_1=await evaluator.retrieval_recall_at_k(ranked, relevant, k=1),
                recall_at_3=await evaluator.retrieval_recall_at_k(ranked, relevant, k=3),
                recall_at_5=await evaluator.retrieval_recall_at_k(ranked, relevant, k=5),
                recall_at_10=await evaluator.retrieval_recall_at_k(ranked, relevant, k=10),
                ndcg_at_1=await evaluator.ndcg_at_k(ranked, relevance_scores, k=1),
                ndcg_at_3=await evaluator.ndcg_at_k(ranked, relevance_scores, k=3),
                ndcg_at_5=await evaluator.ndcg_at_k(ranked, relevance_scores, k=5),
                ndcg_at_10=await evaluator.ndcg_at_k(ranked, relevance_scores, k=10),
            )
            scores.append(qs)

    return scores


async def run_bakeoff(
    fixtures_dir: Path,
    results_dir: Path,
    candidate_keys: list[str] | None = None,
) -> list[CandidateResult]:
    results_dir.mkdir(parents=True, exist_ok=True)

    catalog = []
    for meta_path in sorted(fixtures_dir.rglob("meta.json")):
        with meta_path.open() as f:
            meta = json.load(f)
        if not meta.get("queries"):
            continue
        meta["_fixture_dir"] = str(meta_path.parent)
        catalog.append(meta)

    if not catalog:
        print("No fixtures with queries found.")
        return []

    registry = HandlerRegistry()
    registry.discover()

    corpus_chunks, corpus_ids = await _build_corpus(catalog, registry)
    print(
        f"\nCorpus: {len(corpus_chunks)} chunks from {len(catalog)} fixtures "
        f"(IDs: {corpus_ids[0]} ... {corpus_ids[-1]})"
    )

    candidates = [c for c in CANDIDATES if not candidate_keys or c.key in candidate_keys]
    evaluator = LiveMetricsEvaluator()
    all_results: list[CandidateResult] = []

    for candidate in candidates:
        print(f"\n{'='*60}")
        print(f"Model: {candidate.model_id}")
        print(f"{'='*60}")

        candidate_result = CandidateResult(
            candidate_key=candidate.key,
            model_id=candidate.model_id,
            corpus_size=len(corpus_chunks),
        )

        try:
            loop = asyncio.get_running_loop()
            model = await loop.run_in_executor(None, _load_model, candidate)
            corpus_vecs = await loop.run_in_executor(
                None, _encode, model, [c.content for c in corpus_chunks], ""
            )

            query_scores = await _score_candidate(
                catalog, corpus_chunks, corpus_ids, corpus_vecs, model, candidate, evaluator
            )
            candidate_result.query_scores = query_scores

            for qs in query_scores:
                print(
                    f"  [{qs.fixture_id}] {qs.query[:45]!r}"
                    f"  r@1={qs.recall_at_1}  r@3={qs.recall_at_3}"
                    f"  nDCG@1={qs.ndcg_at_1}  nDCG@3={qs.ndcg_at_3}"
                )

            def _mean(vals):
                filtered = [v for v in vals if v is not None]
                return round(sum(filtered) / len(filtered), 4) if filtered else None

            candidate_result.mean_recall_at_1 = _mean(qs.recall_at_1 for qs in query_scores)
            candidate_result.mean_recall_at_3 = _mean(qs.recall_at_3 for qs in query_scores)
            candidate_result.mean_ndcg_at_1 = _mean(qs.ndcg_at_1 for qs in query_scores)
            candidate_result.mean_ndcg_at_3 = _mean(qs.ndcg_at_3 for qs in query_scores)

            print(
                f"\n  -> mean recall@1={candidate_result.mean_recall_at_1}"
                f"  recall@3={candidate_result.mean_recall_at_3}"
                f"  nDCG@1={candidate_result.mean_ndcg_at_1}"
                f"  nDCG@3={candidate_result.mean_ndcg_at_3}"
            )

        except Exception as exc:
            logger.exception("bakeoff.candidate_failed", candidate=candidate.key)
            candidate_result.error = str(exc)
            print(f"  ERROR: {exc}")

        all_results.append(candidate_result)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / "phase2b-bakeoff-baseline.json"
    payload = {
        "run_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "corpus_size": len(corpus_chunks),
        "corpus_fixtures": [m["fixture_id"] for m in catalog],
        "primary_metric": "recall@1 (pooled corpus)",
        "note": (
            "All fixture chunks pooled into one corpus. k=1 is the primary discriminative metric. "
            "k=10 >= corpus_size so recall@10 is trivially 1.0 — included for completeness."
        ),
        "reference_model": "bge-base",
        "candidates": [
            {
                "key": r.candidate_key,
                "model_id": r.model_id,
                "mean_recall_at_1": r.mean_recall_at_1,
                "mean_recall_at_3": r.mean_recall_at_3,
                "mean_ndcg_at_1": r.mean_ndcg_at_1,
                "mean_ndcg_at_3": r.mean_ndcg_at_3,
                "error": r.error,
                "query_scores": [
                    {
                        "fixture_id": qs.fixture_id,
                        "query": qs.query,
                        "recall_at_1": qs.recall_at_1,
                        "recall_at_3": qs.recall_at_3,
                        "recall_at_5": qs.recall_at_5,
                        "recall_at_10": qs.recall_at_10,
                        "ndcg_at_1": qs.ndcg_at_1,
                        "ndcg_at_3": qs.ndcg_at_3,
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
    )
    parser.add_argument("--fixtures-dir", type=Path, default=Path("eval/fixtures"))
    parser.add_argument("--results-dir", type=Path, default=Path("eval/results"))
    args = parser.parse_args()
    asyncio.run(run_bakeoff(args.fixtures_dir, args.results_dir, args.candidates))


if __name__ == "__main__":
    main()
