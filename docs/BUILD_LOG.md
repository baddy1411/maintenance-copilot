# Build log

Step-by-step record of how the system was built: what each slice did, the decisions made and why, what broke and how it was fixed, and the evidence. Phase gates are summarised in `PROGRESS.md`. Design changes made while building are logged formally in DESIGN.md ("Changes after approval", P2-n). This log tells the story around them.

Conventions: every slice ends with tests passing, CI green, a commit, and a comment on its GitHub issue. Money is spent only with an estimate and a hard cap, agreed first.

---

## Phase 2 · Slice 1: repo hygiene, schema, roles, migrations (#2, #3)
Commits `25af18b`, `d24c74c` · 2026-10-29

**Goal:** a project a stranger can install, plus a database whose permissions enforce the design rather than rely on code discipline.

**Steps**
1. Checked current versions on PyPI for every dependency (FastAPI 0.162, SQLAlchemy 2.1.6, Alembic 1.20.1, psycopg 3.3.6, procrastinate 3.10, langgraph 1.2.18, langgraph-checkpoint-postgres 3.2.0, and others) and pinned them exactly in `pyproject.toml`. `uv.lock` holds the hashes.
2. Chose the database version from **what AWS RDS actually ships**: the AWS release notes list pgvector 0.8.2 on the latest 16, 17 and 18 minors. Pinned `pgvector/pgvector:0.8.2-pg17` by digest for Compose, tests and (later) Terraform. The newer 0.8.7 image would have drifted from RDS (P2-3).
3. Wrote `infra/db/bootstrap.sql` (extension and five login roles) and `init.sh`, which passes passwords in as psql variables so no secret is stored in a file. Compose and the tests run the same script.
4. Inspected the libraries before wrapping them:
   - LangGraph's `PostgresSaver.setup()` is versioned and idempotent, but uses `CREATE INDEX CONCURRENTLY`, which can't run inside Alembic's transaction. So revision 0003 opens its own autocommit connection.
   - procrastinate ships `schema.sql` plus loose upgrade files, with no version table. So every procrastinate bump gets its own Alembic revision (P2-4).
5. Migration 0001: all tables from DESIGN §5.1, the `tools_*` views (the only things `tools_ro` can read), and explicit grants:
   - `audit_log` is append-only for every service role.
   - Only `app_rw` can touch checkpoints.
   - `work_orders` has `CHECK (created_by <> approved_by)`, so the database itself refuses self-approval.
6. While writing it, found a design bug: v2 put `embedding` on `chunks`, which allows only one vector per chunk and breaks the side-by-side embedder change. Moved embeddings to `chunk_embeddings` (P2-1).
7. Tooling:
   - `copilot migrate` checks the pgvector version and is idempotent.
   - Makefile, pre-commit (gitleaks, ruff, mypy strict).
   - CI with GitHub Actions pinned by commit SHA; pip-audit runs without blocking.
   - Dependabot.

**Evidence**
- 15 tests on a real Postgres: role boundaries, the `tools_ro` 5 s timeout (proved with `pg_sleep(6)`), one decision and one work order per run, self-approval refused, and queue enqueue and checkpoint resume as `app_rw`.
- **Mutation check:** temporarily granting UPDATE on `audit_log` made the append-only test fail, then the grant was restored. The tests can catch what they claim to catch.

**Problems:**
- Ruff also reformatted Python code blocks inside `docs/AGENT.md`. Reverted, and markdown excluded.
- Ruff reformatted the spike scripts through pre-commit, because filenames passed explicitly ignore `exclude`. Fixed with `force-exclude`.

## Phase 2 · Slice 2: the LLM gateway (#4)
Commits `7fe0141`, `ffbf746` · 2026-10-29 · spend $0.0025

**Goal:** one place where every model call is checked, priced, redacted and recorded.

**Steps**
1. **Verified the API shapes instead of assuming them.**
   - OpenRouter's OpenAPI spec: `/embeddings` and `/rerank` accept the same `provider` object as chat (`data_collection`, `zdr`). Whether providers enforce it can't be seen from outside, so that stays UNVERIFIED.
   - `/models` lists chat models only. Embedding models are at `/embeddings/models`, and rerank models aren't listed at all (billed per search, $0 per token). So the rerank price comes from config ($0.0025, measured in SPIKE §2b).
   - **Haiku 5.5's listing has a price override: 5x per token at ≥ 100k prompt tokens.** The cost review had assumed one flat price, so the pre-call check now prices by tier (P2-6).
2. Inspected the openai SDK 3.41:
   - It accepts `http_client`.
   - A 402 is a plain `APIStatusError`.
   - Extra response fields (`usage.cost`, `provider`) are kept.
   - It depends on **`httpx2`**, not `httpx`. Passing an `httpx` client only worked by duck typing, and mypy caught it (P2-7).
3. Built `app/llm`:
   - **Catalog:** 1 h TTL; unknown models fail closed.
   - **`strict_schema`:** from the spike.
   - **Parameter filtering:** under `require_parameters`.
   - **Pre-call spend check:** run, user-per-day and global-per-day caps against the `llm_calls` ledger.
   - **Outbound redaction:** emails, phones, SSNs, Luhn-checked cards. Tests also pin that AD numbers, JASC codes, part numbers and registrations pass through untouched.
   - **Error handling:** 402 → `CreditsExhausted`, with no fallback, because the other model shares the account. 404/429/5xx/timeouts → one fallback model.
   - **Structured output:** validation errors are fed back to the model, and the budget doubles on a truncated reply.
4. Decided to send **no user identifier upstream**. OpenRouter's `user` field isn't in the models' supported parameters and could trigger the "no endpoints" 404, and sending nothing is stricter anyway (P2-5).

**Evidence:**
- Recorded-fixture tests for each failure the spike hit: 402, 404 on an unsupported parameter, 429 with Retry-After, truncated JSON, both models down, spend caps.
- Ledger tests on real Postgres.
- Opt-in live test, run with your OK: chat, structured, embed and rerank, **$0.00254**, every call's cost and provider recorded.

**Problem found by the live run:** the catalog crashed on DeepSeek's **time-of-day price overrides**, which have no `min_prompt_tokens`, before any paid call was made. Fix: time-based prices count at their peak in the spend check, and one unreadable listing is skipped instead of breaking the catalog (P2-8).

## Phase 2 · Slice 3: AD ingestion (#5)
Commits `ea60872`, `9dc3565` · 2026-10-29 · spend $0.333

**Goal:** a scheduled, resumable, safe sync of every AD, with metadata good enough to filter and supersede on.

**Steps**
1. Reviewed the spike fetcher, then **measured the text format across all 2,545 ADs before writing regexes.** Findings:
   - the FR document number isn't the AD number;
   - several header formats, typos and two-digit years;
   - 45 AD numbers shared by corrections and republications;
   - R1 revisions are new AD numbers.

   This changed the schema: migration 0004 drops `UNIQUE(ad_number)` and adds `doc_kind`, `jasc` and `applicability` (P2-9).
2. Wrote the parser and iterated against the corpus:
   - AD number 99.5 % → 100 % after the header variants;
   - models 77 % → 85 % after range expansion, the line-wrap fix and numeric designators;
   - applicability 95.4 % → 97.1 % after indented headers.

   Each step was checked by sampling the output, not just the coverage number. For example, the 737 list was missing `737-200` because the FR text had `- 200`.
3. Built `SafeFetcher` (ingestion.md §3), chunking (220/40 words, 450-token cap at a measured 3.97 characters per token, P2-12), per-document transactions with two-way supersession, quarantine, `ingest_runs`, the high-water-mark rule (P2-11), `embed-missing`, and the `sync_ads` periodic job.
4. Saved five real ADs as fixtures (US government works): a supersedes link, a model range, a header typo, a correction and an R1 revision.

**Problems found by the first live run** (8 ADs, `--no-embed`, no spend):
- **NUL bytes** in govinfo text, which Postgres rejects. 276 of 2,545 ADs have them. That one document also crashed the whole run before the run record was closed. Fixes: strip control characters, isolate per-document errors, always close the run record.
- **2026 headers are indented**, which hid applicability in 2 of 8 live ADs.
- **A parser fix didn't reach stored documents**, because unchanged text means skip. The content hash now includes a parser version (P2-10). Bumping it re-processed all 8.

**Backfill**, with your OK (estimate $0.35, cap $0.50):
- 2,545 ADs, 0 quarantined, 0 failed.
- 44,198 chunks, **$0.332**, 33.5 min.
- The database matches the parser measurement exactly, and all 376 `superseded_by` links point to a newer AD.
- The 8 text-only ADs were filled by `embed-missing` ($0.001).

## Phase 2 · Slice 4: handbooks, AC 43.13-1B and uploads (#7)
2026-10-29 · spend $0.045

**Goal:** the four FAA PDFs searchable with citable page numbers, and an upload path that's safe against hostile files.

**Steps**
1. Added pypdfium2 5.14.0 to the app, the same version the spike used (PDFium 156.0.8076.0).
2. **Checked page numbering on the real files.**
   - General and Powerplant carry PDF page labels (`10-25`).
   - Airframe and the AC don't. Their printed numbers are the last token of the page (`8-6`) or a footer (`Page 8-22 Par 8-37`).

   Decision: keep the PDF index (for eval labels and deep links) **and** a printed `page_label` (for citations). Migration 0005.
3. **Took titles from each PDF's own metadata** instead of guessing FAA document numbers: FAA-H-8083-30B, -31B, -32B, and AC 43.13-1B with Change 1 (9/8/98).
4. Built the sandbox (`pdf.py` and `pdf_worker.py`, ingestion.md §5.1): an isolated child process with CPU, memory, file-write and file-count limits, a wall-clock timeout, and a page cap. Its output is validated against the declared page count.
5. Built the shared pipeline for official PDFs and uploads (`pdf_docs.py`):
   - uploads are stored as `<sha256>.pdf`, `restricted` by default;
   - chunks never cross pages;
   - near-empty pages are skipped;
   - **no prefix on handbook chunks**, to stay comparable with the spike's retrieval numbers;
   - large file reads and writes run off the event loop, which ruff's ASYNC240 flagged.
6. Moved the run report, job cap and batch embedding into `common.py`, so both sources share one implementation.

**Problem found by Linux CI** (it passed on macOS): the CPU-limit test got `SIGKILL`, not `SIGXCPU`. With equal soft and hard `RLIMIT_CPU`, Linux sends SIGKILL straight away, which looks the same as an OOM kill. Fix: soft limit N, hard N+5, so SIGXCPU arrives first and the hard limit stays as a backstop. The test also set its own limit instead of calling the worker's, so it now calls `limit_cpu()`. That's why the container target's CI matters even when local tests pass.

**Problem found by tests:** the file-write limit doesn't kill the process. **CPython ignores SIGXFSZ**, so the write fails with EFBIG instead. The guard works (nothing reaches the disk), but the test was asserting the wrong mechanism, so it now checks errno 27 and an empty file.

**Evidence**
- 17 sandbox tests, which simulate a hang, a crash, a segfault, the CPU limit, blocked writes, a corrupt file, a page-count mismatch, a non-PDF and an oversized file. One test runs the real AC 43.13-1B through the sandbox.
- 8 integration tests on Postgres: public official docs with labels, unchanged-skip with no spend, `--only`, a changed PDF re-ingested, an upload stored by hash and restricted, non-PDF and text-less uploads quarantined, and the tools view exposing `access_level`.
- **Live load:**
  - Text first: 4 PDFs fetched from faa.gov, 2,875 pages → 8,935 chunks, 9 near-empty pages skipped (the spike's count). 39 s, no spend.
  - Priced from real character counts: **estimate $0.052**.
  - Embedded under a $0.10 cap: **$0.045**, 108 s.
- **Sanity check** through the agent's read-only role: "chip detector metal particles oil" ranks Powerplant PDF p. 329 (printed 8-2) second on full-text search alone. That's the expected page for the scenario question.

**Corpus now:** 2,549 documents, 53,133 chunks, all embedded, 327 MB. Model spend for the whole project: about $4.12.

## Phase 2 · Slice 5: hybrid retrieval in Postgres (#8)
2026-10-30 · spend $0.20 (evaluation)

**Goal:** the production version of the spike's best retrieval (full-text + vectors + RRF + rerank) in Postgres, proven to match the spike's numbers.

**Steps**
1. Checked that all 19 documents the 40 spike questions point at are in the database, so the question sets can be re-run unchanged.
2. **Vector index.** Migration 0006 builds a partial HNSW index for voyage-4-lite (`halfvec_cosine_ops`, `CONCURRENTLY`, in Alembic's autocommit block): about 12 s, 138 MB.
   - Decision: the model name goes into queries as a validated **literal**. Postgres can't prove a partial-index predicate from a bind parameter under a generic plan.
   - A test shows the index used with the literal and not with a parameter.
3. **Query plans before code.**
   - `EXPLAIN ANALYZE` as `tools_ro` showed the vector query using the partial index through the view, with the access filter on the join: 15 ms.
   - It showed the OR full-text query matching **16,235 chunks (31 %)** for a plain-language question, sequentially scanned: 293 ms with `ts_rank_cd`, 90 ms with `ts_rank`. That confirmed the cost review's finding C10.
4. **Built `app/retrieval/search.py`.**
   - Full-text (three modes, for the experiment) and the embedding call run concurrently.
   - Vector query, RRF, optional supersession demotion, rerank with a timeout.
   - Degraded modes for every external failure.
   - Access control inside both SQL queries.
5. **Tests** with seeded data and a stub gateway that records what would leave the system. They cover:
   - fusion maths;
   - restricted text never returned and never sent to the reranker;
   - demotion;
   - rerank order and its fallback, embedding fallback, missing-index fallback;
   - AND-first mode;
   - partial-index usability.
6. **The evaluation harness** (`scripts/eval_retrieval.py`) re-runs the spike's questions through the real code, caches paid calls (rerank keyed on the candidate *set*), and caps uncached spend.

**What the evaluation found**
- **A bug: per-call config ignored the full-text mode.** Three hybrid variants scored identically even though their full-text halves differ hugely (0.70 vs 0.38). That was the clue. Fixed.
  - The first regression test *passed with the bug put back*, because on the small seeded data both modes return the same order. It was rewritten to record the mode the full-text stage receives, and now fails with the bug and passes without it.
- **Full-text:** OR + `ts_rank` 0.70 vs BM25's 0.68, so the "FTS isn't BM25" risk is resolved. `ts_rank_cd` 0.38. AND-first adds nothing (P2-17).
- **The vector side was losing documents to HNSW approximation.**
  - The fallback scored 0.75 against the spike's 0.85, and a per-question diff showed vector search missing AD chunks the spike's exact search found.
  - Measured against an exact scan: `ef_search` 100 returned 95.1 % of the true top 50, with the worst query at 60 %. At 400: 99.6 % for about 2 ms.
  - Set per query with `SET LOCAL`, with a test that it applies and doesn't leak to the pool (P2-18). Vector recall@5 went 0.75 → 0.80.
  - *My first measurement was wrong.* It reported 1.000 at every setting because psycopg had auto-prepared the statement under `enable_indexscan = off`, and the cached plan survived the `RESET`. The flat 67 ms timings gave it away. Re-measured on separate connections, with no prepared statements and an `EXPLAIN` assertion.
- **Supersession demotion** had no recall effect, slightly lower MRR, and no effect with rerank, so it's off by default (P2-19).
- **The fallback is 0.80, not 0.85.** In every miss the target was a candidate at fused rank 6–10: RRF rewards appearing in both lists over ranking first in one. Rerank recovers them all. The design now states the real number (P2-20).

**Results (40 questions):** hybrid + rerank **0.97 recall@5 / 0.86 MRR** (spike 0.97 / 0.87), scenario set 1.00.

**Latency (laptop):**
- Full-text p95 112 ms, hidden behind the embedding call (p95 656 ms).
- Vector p95 8 ms; rerank p95 0.94–1.22 s.
- Retrieval critical path p95 ≈ **1.8 s**, leaving about 1.2 s of the 3 s time-to-first-token target for the model.

Full write-up: `docs/retrieval.md`.
