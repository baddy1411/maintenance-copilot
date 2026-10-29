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

## Phase 1: Design. In progress

Elephant draft in [DESIGN.md](DESIGN.md) and [ADRs](adr/). Goldfish reviews and the backlog follow; the gate needs your approval of `DESIGN.md`.
