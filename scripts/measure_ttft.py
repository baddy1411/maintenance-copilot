"""Time to first token for the answer-model candidates (DESIGN §8, issue #9).

For each model x reasoning setting it streams real answers: the production prompt, with passages retrieved
for real questions (~2.5k prompt tokens). It records time to the first visible character, total time,
serving provider, cost, and whether the answer's citations pass validation. A rough quality signal, not
an eval.

From a laptop, so absolute numbers include the trip to OpenRouter. In-region numbers come in Phase 8.
Run: uv run python scripts/measure_ttft.py [--reps 5] [--cap 0.10]
"""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any, cast

import httpx2
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.llm import CallContext, Gateway, PostgresLedger, StreamEnd
from app.llm.catalog import estimate_tokens
from app.qa.answer import SourcesSplitter, build_messages, validate_citations, verdict
from app.retrieval.search import Retriever
from scripts.eval_retrieval import OUT, CachingGateway

QUESTIONS = [
    "Drained the oil and the rag is full of shiny bits. Do I pull the engine?",
    "How many broken strands in a control cable mean it has to be replaced?",
    "How should safety wire be installed on a turnbuckle?",
    "What does a magnetic plug in the turbine engine oil return line tell the mechanic?",
    "Pedal goes almost to the floor and feels soft after a brake job. What did I miss?",
]
# Haiku at "medium" isn't listed: with max_tokens 900 it spent the whole budget reasoning and showed no text
# in 5 of 5 runs (docs/qa.md). Pass --all to include it.
CONFIGS = [("anthropic/claude-haiku-5.5", "none"), ("openai/gpt-6-luna", "none"), ("openai/gpt-6-luna", "medium")]
ALL_CONFIGS = [*CONFIGS, ("anthropic/claude-haiku-5.5", "medium")]


def pct(xs: list[float], p: float) -> float:
    return round(sorted(xs)[max(0, int(len(xs) * p + 0.5) - 1)], 0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--cap", type=float, default=0.10)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    s = Settings()
    async with (
        AsyncConnectionPool(s.dsn("tools_ro"), open=False) as ro,
        AsyncConnectionPool(s.dsn("app_rw"), open=False) as rw,
        httpx2.AsyncClient() as http,
    ):
        gw = Gateway(s, http, PostgresLedger(rw))
        # Retrieval goes through the eval cache, with a small cap so questions that aren't cached get real
        # (not degraded) retrieval. Answers built on degraded retrieval would skew the verdict counts.
        retriever = Retriever(ro, cast(Gateway, CachingGateway(gw, 0.05, OUT / "cache.json")))
        prompts = []
        for q in QUESTIONS:
            res = await retriever.search(q, CallContext(kind="ttft_retrieval"))
            assert not res.degraded, f"degraded retrieval for {q!r}: {res.degraded}"
            prompts.append((q, res.hits, build_messages(q, res.hits)))
        tokens = [estimate_tokens(len(json.dumps(m))) for _, _, m in prompts]
        print(f"prompt size: ~{statistics.mean(tokens):.0f} tokens (pessimistic chars/3 estimate)")

        spent, report = 0.0, {}
        for model, effort in ALL_CONFIGS if args.all else CONFIGS:
            ttft, total, valid, providers, silent = [], [], [], set(), 0
            for rep in range(args.reps):
                q, hits, msgs = prompts[rep % len(prompts)]
                if spent >= args.cap:
                    raise SystemExit(f"cap ${args.cap:.2f} reached")
                t0 = time.perf_counter()
                first: float | None = None
                splitter = SourcesSplitter()
                async for item in gw.stream_chat(CallContext(kind="ttft"), msgs, max_tokens=s.answer_max_tokens,
                                                 model=model, reasoning={"effort": effort}):  # fmt: skip
                    if isinstance(item, StreamEnd):
                        spent += item.cost
                        providers.add(item.provider)
                        continue
                    if item and first is None:
                        first = time.perf_counter() - t0  # the model's first visible character
                    splitter.feed(item)
                total.append((time.perf_counter() - t0) * 1000)
                if first is None:
                    silent += 1  # no visible text at all (e.g. the whole budget spent on reasoning)
                else:
                    ttft.append(first * 1000)
                cites, _ = validate_citations(splitter.sources, hits)
                valid.append(verdict(splitter.answer, cites)[0])
            row: dict[str, Any] = {
                "ttft_ms": {"p50": pct(ttft, 0.5), "max": round(max(ttft))} if ttft else None,
                "no_visible_output": silent,
                "total_ms": {"p50": pct(total, 0.5), "max": round(max(total))},
                "verdicts": {v: valid.count(v) for v in set(valid)},
                "providers": sorted(p for p in providers if p),
            }
            report[f"{model} reasoning={effort}"] = row
            t = row["ttft_ms"]
            ttft_txt = f"TTFT p50 {t['p50']:>6.0f} ms (max {t['max']:>5})" if t else "TTFT    n/a      "
            print(f"{model:28s} reasoning={effort:6s} {ttft_txt} | silent {silent} | total p50 "
                  f"{row['total_ms']['p50']:>6.0f} ms | {row['verdicts']} | {row['providers']}")  # fmt: skip
        stamp = time.strftime("%Y%m%d-%H%M%S")
        payload = json.dumps({"results": report, "spent_usd": round(spent, 4)}, indent=2)
        await asyncio.to_thread(Path(OUT / f"ttft-{stamp}.json").write_text, payload)
        print(f"spent ${spent:.4f}; wrote data/eval/ttft-{stamp}.json")


if __name__ == "__main__":
    asyncio.run(main())
