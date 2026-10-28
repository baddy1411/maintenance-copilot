# Spike findings (Phase 0)

Status: **for gate review**. Run 2026-10-28. The code is throwaway (`spikes/`). Every number below comes from a run whose command is listed, and each result is marked VERIFIED (it ran and we saw the output) or UNVERIFIED.

Total LLM and embedding spend for the whole spike: see the bottom of this file (the hard cap was $1.00 and is enforced in `spikes/common.py` from OpenRouter's real `usage.cost`).

## 1. Tool calling through OpenRouter (`s1_tool_calling.py`) VERIFIED

**Setup.** There are three tools (`search_documents`, `query_sdr`, `get_aircraft`), defined as Pydantic models and exported as strict JSON Schema. There are 8 prompts, each with a check on the tool *and* its arguments, including one "don't call a tool" prompt and one trap: a registration that isn't in our fleet. Each prompt runs 2 times, so 16 tool calls per model. There is also one structured-output task: produce a `Diagnosis` that cites the evidence IDs, validated by Pydantic. The provider settings are `data_collection: deny` and `require_parameters: true`.

Command: `uv run python s1_tool_calling.py --repeats 2`

| Model (OpenRouter ID) | Tool cases | Structured `Diagnosis` | p50 / max latency (tool call) | Avg cost per tool call | Price in / out per M tokens |
| --- | --- | --- | --- | --- | --- |
| `anthropic/claude-haiku-5.5` | **16/16** | **2/2** | 1.34 s / 2.80 s | $0.00027 | $0.10 / $0.50 |
| `openai/gpt-6-luna` | **16/16** | **2/2** | 1.55 s / 3.05 s | $0.00008 | $0.10 / $0.50 |
| `deepseek/deepseek-v4.1-flash` | 12/16 | 2/2 | 0.79 s / 1.22 s | $0.00028 | $0.04 / $0.50 |

**DeepSeek failures (real):**
- It ignored "top 3" and used `top_k: 5` (2 of 2 runs).
- **It invented a fleet registration.** Asked about N12345, which isn't ours, it called `get_aircraft` with `MX-123` in one run and `MX-001` in another (2 of 4 runs across both executions). For a maintenance tool, silently swapping the aircraft is a serious failure. It's a strong reason against DeepSeek as the primary model, and a test case the Phase 3 golden set must keep.
- It sent invalid arguments for the aggregation query once.
- In an earlier run it added an unknown `query` field to `query_sdr`. Strict schemas now reject that.

**Caveat:** n = 16 per model is a smoke test, not a benchmark. Phase 3 makes the real comparison on 100+ cases.

### Integration lessons (each one cost a failed run)

1. **`require_parameters: true` returns 404 when you send a parameter the model doesn't support.** Haiku 5.5 and GPT-6 Luna support neither `temperature` nor `parallel_tool_calls`. The fix: read `supported_parameters` from `GET /api/v1/models` and send only the ones that are supported (`common.optional`). We keep `require_parameters: true`, because silently dropped parameters are worse than a loud 404.
2. **OpenAI strict mode rejects Pydantic's default schema.** It needs `additionalProperties: false` and every property in `required`, with optional fields made nullable. We added a `strict_schema()` converter. The same strict schemas on tools also stop extra fields.
3. **Reasoning tokens count against `max_tokens`.** DeepSeek spent 1,371 of 1,500 tokens reasoning and then cut its JSON off mid-object (`finish_reason: length`). The fix: allow more `max_tokens` for structured calls and send `reasoning: {effort: "low"}` via `extra_body` (it isn't an OpenAI SDK argument).
4. **Always set `max_tokens`.** Without it, OpenRouter reserves credit for the model's full output length and returns 402 on a low balance.
5. Cost per request comes back in `usage.cost` on every response, with no flag needed (OpenRouter docs, checked 2026-10-28). The ledger uses that real number, not an estimate.

## 2. Retrieval over the real corpus (`s2_retrieval.py`) VERIFIED

**Corpus:** the full spike corpus, **49,871 chunks from 2,549 documents** (4 FAA handbooks plus 2,545 ADs), about 10.6M words. Chunks are 220 words with a 40-word overlap, and each AD chunk is prefixed with the AD title so make and model survive chunking. Parsing takes 8–17 s and the BM25 index builds in 2 s.

**Questions:** 18 hand-written questions (`spikes/retrieval_questions.json`): 11 against specific handbook or AC pages, which I read to set the expected page, and 7 against specific 2026 ADs. A hit means a top-5 chunk comes from the right document and, for PDFs, from an expected page.

Command: `uv run python s2_retrieval.py --k 5` (full run 2026-10-29, 8 min 51 s, mostly embedding)

| Method | recall@5 | MRR@5 |
| --- | --- | --- |
| BM25 | **0.89** (16/18) | **0.75** |
| Dense (`voyageai/voyage-4-lite`) | 0.78 (14/18) | 0.61 |
| Hybrid (RRF of BM25 + dense, k=60) | **0.89** (16/18) | **0.75** |

**My expectation that dense retrieval would fix the paraphrase misses only partly held:**
- "Which NDI method uses a *glowing dye under black light*…" (the page says "fluorescent penetrant"): **missed by all three methods.**
- "What does a *magnetic plug* in the oil return line tell the mechanic?" (the page says "chip detector"): dense found it **at rank 5**, but hybrid lost it again because BM25 ranked it outside its top 50.
- Dense missed 3 questions that BM25 got at rank 1 or 2 (lockwire, turbine hot start, the ATR42 limitation). Exact technical terms ("safety wire", "EGT", model numbers) carry most of the signal in this corpus.
- Hybrid improved ranking on some questions (eddy current 3→1, Dash 8 AD 4→1, human factors 2→1) and lost a place on others (787 couplings 1→2, where an older superseded AD `2023-11091` outranked the current one). The overall score tied BM25.

**What this means for Phase 1 and 3:** keep hybrid search, because it never did worse than dense and it fixed several ranking errors. But the spike gives **no evidence yet that the cheap embedding model is worth it over BM25 alone.** Things to test in Phase 3, on a less keyword-biased golden set:
1. A stronger embedding model (`voyage-4` or `voyage-4-large`, at 3–6x the price).
2. A reranker on the top 50.
3. A tuned RRF weighting.
4. Query rewriting, so plain-language terms map to technical vocabulary (for example "glowing dye" to "fluorescent penetrant").

AD **supersession** matters: older ADs on the same topic compete with current ones, so we need metadata that boosts the current AD and links superseded ones.

Other observations:
- AD questions work well by keyword (6/7 at rank 1 or 2), because ADs name specific makes, parts and failure modes. The Dash 8 question ranked at 4, behind an older related AD (`2025-18757`). That's correct behaviour, but answer generation will need recency and supersession awareness.
- **Bias caveat:** I wrote the questions after reading the target pages, which favours keyword overlap and probably overstates BM25. The Phase 3 golden set needs questions written from technician scenarios, not from the pages.

**The first dense run was stopped by account credit.** It embedded 400 batches until OpenRouter returned `402 Insufficient credits. This account never purchased credits` at a cumulative spend of $0.213. Those vectors were lost because the script only saved at the end. **Fixed:** `embed()` now saves each batch of 64 as it lands and resumes from those batches. After credits were added, the rerun embedded the full corpus for **$0.36**. My estimate of $0.29 came from words × 1.35, which undercounts tokens on this technical text by about 25%. Phase 4 cost estimates should use a real tokenizer count, not a word ratio.

## 3. Human-approval interrupt (`s3_interrupt.py`) VERIFIED

LangGraph 1.2.18, `interrupt()` plus `Command(resume=...)`, with the `InMemorySaver` checkpointer. No LLM; the diagnosis is stubbed so the test only exercises the control flow.

Command: `uv run python s3_interrupt.py`, result **7/7 checks passed**:

| Check | Result |
| --- | --- |
| Run pauses at the approval interrupt, and the payload carries the draft | PASS |
| Nothing is written before approval | PASS (0 rows) |
| Approve writes exactly one work order | PASS |
| Same request retried on a **new thread** is deduplicated by the idempotency key | PASS (still 1 row) |
| Reject writes nothing | PASS |
| A freshly compiled graph (simulating another API worker) sees the pending interrupt | PASS |
| … and can resume it and write | PASS |

The write is a **graph node, not an LLM tool**, so the model cannot skip approval. Phase 2 must swap `InMemorySaver` for a durable Postgres checkpointer. The cross-worker check here shares one in-memory saver, so **resume across a real process restart is UNVERIFIED**.

## 4. Data pipeline findings VERIFIED

- **The corpus is live and large.** We have 2,545 ADs from 2020 to 2026 (the 2026 batch took 2 min 05 s for 292, about 140 per minute; the 2020–25 backfill wasn't timed), plus 182,910 SDRs for 2024–2026 and 2,875 handbook pages. Volumes are in `docs/DATA.md`.
- **A block page was silently stored as a document.** federalregister.gov's `raw_text_url` returns a "Request Access" bot-check page to scripts, and the first fetcher saved 235 of them as if they were ADs. The fix: take the text from govinfo.gov (GPO, the same official text) and **validate content before storing** (it must contain "Airworthiness Directive" and must not be a block page). Phase 2 ingestion needs the same kind of check.
- **We don't get around bot blocks.** faa.gov and nhtsa.gov HTML pages return 403. We use only endpoints that allow scripts.
- **pypdf crashes on the FAA handbooks** (`LimitReachedError: maximum number of tokens per stream`). pypdfium2 parses all 2,875 pages in about 7 s, and only 9 pages are near-empty. PyMuPDF was avoided because of its AGPL licence. This goes in a Phase 1 ADR.
- **SDR model names need normalising.** The B737-800 shows up as `737823`, `7378H4`, `737824` and more (Boeing customer codes), and Bombardier appears as `CNDAIR`. The `query_sdr` tool needs an alias table, because models pass "737-800" and "Bombardier" (seen in the spike). Without one, filters would return nothing.

## 5. Model shortlist for Phase 3

| Role | Candidates | Why |
| --- | --- | --- |
| Primary (agent + answers) | `anthropic/claude-haiku-5.5`, `openai/gpt-6-luna` | Both were perfect on the smoke test at the same list price. Luna used about 3x fewer prompt tokens per call here (525 vs 1,578, so tool-definition overhead differs by provider) and was the cheapest per call. |
| Cheap or third comparison | `deepseek/deepseek-v4.1-flash` | Fastest and cheapest per input token, but it invented a registration. Kept for Phase 3 as the comparison point, not favoured. |
| Strong tier for routing (Phase 4) | `anthropic/claude-sonnet-5.5` or `openai/gpt-6.1-terra` | Not tested yet (UNVERIFIED). Phase 4 decides whether multi-step diagnosis needs one. |

Pinning: Phase 3 pins the upstream provider with `provider.order` plus `allow_fallbacks: false` for reproducible evals. The spike didn't pin. Luna was served by Azure and then OpenAI across calls, which the error metadata shows.

## Spend

**$0.5724 total** (ledger `spikes/.cache/spend.jsonl`, from `usage.cost`):
- **$0.0387** on 156 logged chat completions.
- **$0.5337** on 1,181 embedding batches. Of that, $0.174 was the first run, which stopped at the 402 and lost its vectors, and $0.36 was the complete rerun.

About a third of the total was waste from the lost partial run. Resumable embedding is now built in. A few diagnostic probes run outside the scripts weren't logged. Their costs, as returned in `usage.cost`, total under $0.001. The $1.00 cap was never reached.

## Exit gate status

| Gate item | Status |
| --- | --- |
| `SCOPE.md` and `DATA.md` drafted | Done, **awaiting your approval** |
| Spike: tool calling via OpenRouter | **Passed.** Two models at 16/16 |
| Spike: approval interrupt | **Passed.** 7/7 |
| Spike: retrieval returns the right passages | **Passed.** Hybrid and BM25 at 0.89 recall@5 over 49,871 chunks. Dense alone scored lower (0.78); see section 2 |
| Model shortlist | Done (section 5) |

## Open items for the gate

- **Rotate the OpenRouter key.** It was pasted in chat and is still the active key.
- Approve the domain switch and the two scope questions in `docs/SCOPE.md`.
