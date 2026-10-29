# Progress

One entry per phase gate. Evidence is linked; anything not measured is marked UNVERIFIED.

## Phase 0: Scope and spike. Closed 2026-10-29 (tag `phase-0`)

| Gate item | Result | Evidence |
| --- | --- | --- |
| `SCOPE.md`, `DATA.md` approved | Approved by "go ahead" on 2026-10-29. Domain: aviation maintenance on live FAA data | [SCOPE](SCOPE.md), [DATA](DATA.md) |
| Tool calling via OpenRouter | Haiku 5.5 and GPT-6 Luna 16/16; DeepSeek V4.1 Flash 12/16 (invented a registration) | [SPIKE §1](SPIKE.md) |
| Approval interrupt | 7/7 checks; restart across processes UNVERIFIED (Phase 2) | [SPIKE §3](SPIKE.md) |
| Retrieval | Hybrid + rerank 0.97 recall@5 / 0.87 MRR over 49,871 chunks (40 questions); BM25 0.50 on scenario questions | [SPIKE §2, §2b](SPIKE.md) |
| Model shortlist | Haiku 5.5, GPT-6 Luna; DeepSeek as comparison | [SPIKE §5](SPIKE.md) |
| Spend | $3.7443 of a $5 cap | [SPIKE: Spend](SPIKE.md) |

Carried forward:
- **Rotate the OpenRouter key** (it was pasted in chat).
- The NDI "glowing dye" question misses in every configuration. Check the label in Phase 3.
- Postgres FTS vs `rank_bm25` parity (Phase 2).

## Phase 1: Design. Closed 2026-10-29 (tag `phase-1`), DESIGN v2 approved by you

| Gate item | Result | Evidence |
| --- | --- | --- |
| `DESIGN.md` with service-level targets | DRAFT v2 | [DESIGN](DESIGN.md) |
| ADRs | 0001–0009 (queue, retrieval, PDF parser, agent execution, hosting, observability, evals, SQL tools, no Redis) | [adr/](adr/) |
| Goldfish reviews | Three fresh-context reviews (security, cost and scale, operations): 36 findings. 31 accepted, 5 accepted with changes. One part of O9 was rejected: MCP stays a separate entrypoint. Each is logged with where it landed | [Review log](DESIGN.md#review-log) |
| Backlog | 29 issues labelled `phase-1` … `phase-9` | [Issues](https://github.com/baddy1411/maintenance-copilot/issues) |
| Your approval of `DESIGN.md` | Approved 2026-10-29 ("Approve, start Phase 2") | — |
| Rotate the OpenRouter key | **Pending (yours)**, issue #1 | — |

The two structural changes from the reviews: agent runs execute as worker jobs instead of inside the request (ADR-0004), and Redis is removed (ADR-0009).

## Phase 2: Foundation. In progress

| Slice | Issue | State | Evidence |
| --- | --- | --- | --- |
| Repo hygiene, CI | #2 | Done (`make up`/`make eval` come with #15 and Phase 3) | CI green on every push |
| Schema, roles, `migrate` | #3 | Done | `tests/test_schema.py` |
| LLM gateway | #4 | Done; live smoke test $0.00254 | `tests/test_llm_*.py` |
| AD ingestion | #5 | Done; **full 2020–2026 backfill loaded locally** | `tests/test_ingest_*.py`, below |

**Local corpus after the AD backfill (2026-10-29):**
- 2,545 ADs (2020-01-06 to 2026-10-27): 11 corrections, 10 revisions, 376 marked superseded (all pointing to a newer AD).
- 44,198 chunks, all embedded with voyage-4-lite. Average 1,405 characters, none over the 450-token cap.
- 280 MB database.
- The run took 33.5 min: 0 quarantined, 0 failed.

**Model spend to date:**

| Item | Cost |
| --- | --- |
| Phase 0 spikes | $3.7443 |
| Gateway live smoke test | $0.0025 |
| AD backfill | $0.3319 (estimate $0.35, cap $0.50) |
| `embed-missing` for the 8 text-only ADs | $0.0011 |
| **Total** | **≈ $4.08** |
