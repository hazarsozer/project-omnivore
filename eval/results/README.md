# eval/results — Eval Harness Output

## What lives here

| File | Scope | Measures |
|------|-------|----------|
| `phase2-extraction-baseline.json` | Phase 2 | Structural extraction: fragment count, table count, chunk count, handler match. All retrieval metrics (`recall@10`, `ndcg@10`) are intentionally `null` — see below. |
| `YYYYMMDDTHHMMSSZ.json` | Any run | Timestamped outputs from ad-hoc harness runs. |

## Why recall@10 is null

`eval/metrics.py` exposes only `StubMetricsEvaluator`. All retrieval metrics return `None` with a `# Phase 3` comment. The current harness (`eval/runner.py`) uses `_DiskCtx`, which reads fixtures from disk and never touches Postgres, pgvector, or the BGE model. It only measures extraction fidelity.

This is intentional. Recall@10 baselining is deferred to **Phase 2b**, which already owns:
- The embedding bakeoff (BGE-base-en-v1.5 vs BGE-M3 vs nomic-embed)
- GPU worker infrastructure
- Retrieval gold-set fixtures with query/relevance judgment pairs

A single-model recall@10 "baseline" without comparators is not informative. The bakeoff is where comparators exist — that is the right place to baseline and regress against.

## Phase 2b recall@10 plan (for reference)

When Phase 2b begins:

1. Add `queries: [{query: str, relevant_fragment_ids: [str]}]` to each fixture's `meta.json`.
2. Implement `LiveMetricsEvaluator` in `eval/metrics.py` (replacing `StubMetricsEvaluator`).
3. Implement a live runner that:
   - Ingests fixture content into a real Postgres + pgvector container.
   - Embeds chunks with the passage-side prefix (empty string).
   - Embeds queries with `QUERY_PREFIX` from `omnivore.pipeline.embeddings`.
   - Runs the search SQL for each query.
   - Computes recall@10 and nDCG@10 against relevance judgments.
4. Record `phase2b-bakeoff-baseline.json` for each candidate model.
5. BGE-base-en-v1.5 is the **reference** — other models must beat it by ≥ 10% recall@10 to justify the switch.
