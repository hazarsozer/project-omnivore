# eval/results — Eval Harness Output

## What lives here

| File | Scope | Measures |
|------|-------|----------|
| `phase2-extraction-baseline.json` | Phase 2 | Structural extraction: fragment count, table count, chunk count, handler match. Retrieval metrics are `null` — not in scope for Phase 2. |
| `phase2b-bakeoff-baseline.json` | Phase 2b/2c | Embedding bakeoff: recall@k and nDCG@k for k∈{1,3,5,10} across BGE-base, BGE-M3, nomic-embed. Pooled-corpus design. Primary metric: recall@1. Updated 2026-05-08 with 53-chunk corpus. |
| `YYYYMMDDTHHMMSSZ.json` | Any run | Timestamped outputs from ad-hoc harness runs. |

## Bakeoff methodology

**Design:** All fixture chunks are pooled into one corpus. Each query is scored against
the full corpus. This makes recall@1 discriminative — a model must rank the correct chunk
above all others to score 1.0.

**Why pooled corpus:** Per-fixture evaluation (previous design) scored each query against
only the fixture's own chunks. With k=10 > chunks-per-fixture, recall@10 was
mathematically guaranteed to be 1.0 for *any* model. The pooled design fixes this.

**Why recall@1 is the primary metric:** "Did the model rank the most relevant chunk
first?" is the most direct measure of embedding quality for retrieval use cases.

**Decision rule:** BGE-M3 must beat BGE-base by ≥ 10% on recall@1 to justify the
destructive migration (vector(768) → vector(1024) + HNSW rebuild).

---

## Phase 2b initial bakeoff (2026-05-07) — 9 chunks

Corpus: 9 chunks across html-001, md-001, txt-001 (3 fixtures).

| Model | recall@1 | recall@3 | nDCG@1 | nDCG@3 |
|-------|----------|----------|--------|--------|
| BGE-base-en-v1.5 (reference) | 0.2963 | 0.6574 | 0.7778 | 0.6884 |
| **BGE-M3** | **0.3518** | **0.9444** | **0.8889** | **0.9590** |
| nomic-embed-text-v1.5 | 0.2407 | 0.5741 | 0.6667 | 0.6317 |

BGE-M3 showed +18.7% recall@1 over BGE-base — above the 10% threshold. Switch was
held pending a larger corpus for statistical confidence.

---

## Phase 2c expanded bakeoff (2026-05-08) — 53 chunks

Corpus: 53 chunks across 7 fixtures (html-001, md-001, md-002, md-003, md-004, txt-001,
txt-002). Added 4 new fixtures covering Python asyncio, PostgreSQL internals, distributed
systems patterns, and ML engineering — 39 queries total.

| Model | recall@1 | recall@3 | nDCG@1 | nDCG@3 |
|-------|----------|----------|--------|--------|
| **BGE-base-en-v1.5 (reference)** | **0.361** | 0.6505 | 0.7179 | 0.7247 |
| BGE-M3 | 0.332 | **0.6997** | **0.7436** | **0.762** |
| nomic-embed-text-v1.5 | 0.312 | 0.5947 | 0.6667 | 0.6577 |

### Key finding: BGE-M3 switch is BLOCKED

- **recall@1**: BGE-base leads (0.361 > 0.332, BGE-M3 is −8.0%)
- **recall@3**: BGE-M3 leads by +7.6% (below 10% threshold)
- **nDCG@1**: BGE-M3 leads by +3.6% (below 10% threshold)
- **nDCG@3**: BGE-M3 leads by +5.1% (below 10% threshold)

**BGE-M3 does not clear the 10% recall@1 threshold on the 53-chunk corpus.** The
9-chunk result (+18.7%) was a statistical artifact of an under-constrained evaluation.
The destructive DB migration is NOT justified. BGE-base-en-v1.5 (768-dim) remains
the production embedding model. Decision logged in `CLAUDE.md §Q19`.

**nomic-embed:** Consistently underperforms both alternatives. Not viable.

---

## Limitations

- Plain-text fixtures (txt-001, txt-002) produce few chunks (1 and 3 respectively)
  because the text handler generates flat fragments with no heading structure, so the
  chunker only splits on token overflow. The short chunks compress the evaluation signal
  from those fixtures.
- Only `["en"]` language tested. Multilingual evaluation deferred.
- No hybrid-search evaluation (BM25 + vector + RRF). Bakeoff measures pure vector
  search in isolation.
