# eval/results — Eval Harness Output

## What lives here

| File | Scope | Measures |
|------|-------|----------|
| `phase2-extraction-baseline.json` | Phase 2 | Structural extraction: fragment count, table count, chunk count, handler match. Retrieval metrics are `null` — not in scope for Phase 2. |
| `phase2b-bakeoff-baseline.json` | Phase 2b | Embedding bakeoff: recall@k and nDCG@k for k∈{1,3,5,10} across BGE-base, BGE-M3, nomic-embed. Pooled-corpus design. Primary metric: recall@1. |
| `YYYYMMDDTHHMMSSZ.json` | Any run | Timestamped outputs from ad-hoc harness runs. |

## Phase 2b bakeoff methodology

**Design:** All fixture chunks (9 total across html-001, md-001, txt-001) are pooled into one
corpus. Each query is scored against the full 9-chunk corpus. This makes recall@1
discriminative — a model must rank the correct chunk above 8 others to score 1.0.

**Why pooled corpus:** Earlier design (previous commit) scored each query against only
the fixture's own chunks. With k=10 > chunks-per-fixture, recall@10 was mathematically
guaranteed to be 1.0 for *any* embedding model (including random). The pooled design
fixes this.

**Why recall@1 is the primary metric:** With 9 chunks in the corpus, k=1 captures
"did the model rank the most relevant chunk first?" — the most direct measure of
embedding quality.

## Phase 2b bakeoff results (2026-05-07)

| Model | recall@1 | recall@3 | nDCG@1 | nDCG@3 |
|-------|----------|----------|--------|--------|
| BGE-base-en-v1.5 (reference) | 0.2963 | 0.6574 | 0.7778 | 0.6884 |
| **BGE-M3** | **0.3518** | **0.9444** | **0.8889** | **0.9590** |
| nomic-embed-text-v1.5 | 0.2407 | 0.5741 | 0.6667 | 0.6317 |

**Decision rule:** A model must beat BGE-base by ≥ 10% on recall@1 to justify switching
(dimensional cost of a DB migration to resize the vector column).

**BGE-M3 result:** +18.7% recall@1 vs BGE-base. **Exceeds the 10% threshold.**
However, switching is currently blocked because:
1. Only 9 chunks in the corpus — statistically thin. Add larger fixtures before deciding.
2. BGE-M3 outputs 1024-dim vectors. Current schema has `vector(768)`. Switching requires
   a destructive migration (drop + rebuild embeddings column + rebuild HNSW index).
3. Decision logged in `docs/architecture.md §9 Q19`.

**nomic-embed result:** Underperforms BGE-base by 18.8% recall@1. Not a viable switch.

## Limitations of the current bakeoff

- 9 chunks is a thin corpus. The evaluation needs fixtures with 50+ chunks for robust results.
- Queries target specific facts that exist in only 1-2 chunks — good for precision, but
  doesn't test recall of broader topics.
- Only `["en"]` language tested. Multilingual evaluation deferred to Phase 2c.
- No hybrid-search evaluation (BM25 + vector + RRF). The bakeoff measures pure vector
  search in isolation.
