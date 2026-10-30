# ADR-0002: Hybrid retrieval in Postgres, with API embeddings and a reranker

Status: **Accepted** · 2026-10-30, implemented in #8 · revised in DESIGN v2 (cost review C1 and C4, ops review O4). The numbers come from `docs/SPIKE.md` §2 and §2b and are re-confirmed on the Phase 3 golden set.

## Context
The corpus has 49,871 chunks in the spike and will grow to about 200k with the full AD history. Users ask in plain technician language ("shiny bits in oil"), but the sources use formal terms ("metal particles, chip detector") and exact identifiers (AD numbers, part numbers, models). We need recall@5 ≥ 0.85 within a p95 time to first token of 3 s and a mean cost of ≤ $0.01 per Q&A. There is one datastore (Postgres) and no GPU.

## Options

**Where the index lives**

| Option | Pros | Cons |
| --- | --- | --- |
| **Postgres: pgvector HNSW + full-text (GIN)** | One datastore, transactional with ingestion, same in every environment, RDS supports pgvector | `ts_rank_cd` isn't BM25 (risk in DESIGN §9); HNSW memory grows with dimension × rows |
| OpenSearch / Elasticsearch | Real BM25, mature hybrid search | A second stateful cluster to run and pay for; ingestion is no longer one transaction |
| Dedicated vector DB (Qdrant etc.) | Fast ANN, filtering | A third system; keyword search still needed elsewhere |

**Pipeline**, measured on 40 questions (18 page-biased, 22 scenario), recall@5 / MRR@5 over all 40:

| Pipeline | All | Scenario only | Added cost per query | Added latency (p50) |
| --- | --- | --- | --- | --- |
| BM25 only | 0.68 / 0.54 | 0.50 | $0 | — |
| Hybrid, voyage-4-lite | 0.85 / 0.64 | 0.82 | ~$0.00004 | ~0.3–0.9 s embed |
| Hybrid + LLM rewrite, lite | 0.95 / 0.71 | 0.95 | +$0.00007 | +1.6 s |
| **Hybrid + rerank-4-pro, lite** | **0.97 / 0.87** | **1.00** | +$0.0025 | +0.9 s (p95 1.3 s) |
| Hybrid + rerank, voyage-4-large | 0.97 / 0.88 | 1.00 | same per query; corpus embed 6x ($2.16 vs $0.36) | same |
| Hybrid + rewrite + rerank, lite | 0.95 / 0.85 | 1.00 | +$0.0026 | +2.5 s |

## Decision
- **Index:** Postgres. Full-text search uses GIN on a generated `tsvector`. Vectors use pgvector **`halfvec(1024)`** with **one partial HNSW index per `embed_model`** (cosine), `ef_search` = 100 and iterative scan. Indexes are built after bulk load. SDR rows are FTS-only. (The halfvec, partial index, ef_search and SDR choices were added after the cost and ops reviews.)
- **Embedder:** `voyageai/voyage-4-lite` through OpenRouter. The active model is a database row (DESIGN §8.1), so a re-embed can run side by side and the switch is gated by an eval.
- **Query pipeline:** FTS top 50 and vector top 50, with the access filter applied inside the SQL → RRF (k=60) → superseded ADs demoted → **`cohere/rerank-4-pro` on the fused top 50**, scored against the original question → top k=5 into the answer context, or 8 when the question names an AD or a model.
- **No query rewriting for Q&A.** It added nothing once reranking was on, and it costs 1.6 s before retrieval can start. **If the reranker fails or takes more than 1.5 s, Q&A uses the RRF order** (0.85 recall in the spike), so time to first token stays on budget. v1 fell back to rewriting here, which the cost review showed takes about 5.5 s. Rewriting stays as the rerank fallback on the agent path only, where it helped (+0.10 recall) and time to first token doesn't apply.

## Consequences
- The reranker dominates per-query retrieval cost ($0.0025 out of the $0.01 Q&A budget) and adds about 1 s. It sits on the critical path of time to first token, so its 2 s timeout and fallback matter.
- Rerank and embedding calls send question text and chunk text (all public FAA material) to OpenRouter and Cohere. User questions may contain registrations or names, so the same redaction and `data_collection: deny` rules apply (`DATA.md`).
- Phase 2 must re-run both spike question sets against the Postgres implementation. If FTS-side recall drops noticeably from the `rank_bm25` numbers, look at a BM25 extension supported on RDS, or BM25 computed in the app.
- The choice of lite over large rests on a single-question difference at n=40. Phase 3 re-runs the comparison on ≥ 150 questions. Switching embedders is a re-embed job (~$0.36 for lite or ~$2.16 for large per spike-size corpus, more for full history), not a schema change, as long as the dimension stays at 1024.
- A self-hosted reranker (for example bge-reranker on CPU) is the fallback if API rerank cost or latency becomes a problem. Not measured yet (UNVERIFIED).

## Implementation results (issue #8)
Re-running the spike's 40 questions on the Postgres implementation (`docs/retrieval.md`):
- **Hybrid + rerank: 0.97 recall@5 / 0.86 MRR** (spike 0.97 / 0.87); scenario set 1.00. The decision holds.
- Full-text: OR + `ts_rank` 0.70 (BM25 0.68), so the risk that Postgres FTS isn't BM25 is resolved. `ts_rank_cd` 0.38.
- HNSW needed `ef_search` 400. At 100 it returned only 95.1 % of the exact top 50.
- The RRF-order fallback is 0.80, not 0.85. Supersession demotion is off (no measured benefit).
- HNSW index: about 12 s to build, 138 MB at 53k vectors. Retrieval critical path p95 ≈ 1.8 s from a laptop.
