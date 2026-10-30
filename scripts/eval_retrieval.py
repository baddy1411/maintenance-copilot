"""Re-run the spike's 40 retrieval questions against the Postgres implementation (issue #8).

Same questions and scoring as spikes/s2_retrieval.py: a hit is a top-k chunk from the expected document
and, for PDFs, from an expected page. Results go to data/eval/ (git-ignored) and are summarised in
docs/retrieval.md.

Paid calls (query embeddings, rerank) go through the real gateway and are cached in data/eval/cache.json,
so re-runs and variants don't pay again. Rerank results are keyed on the *set* of candidates, because the
reranker scores each document independently of the order it's given. --cap bounds uncached spend.

Run: uv run python scripts/eval_retrieval.py [--variants ...] [--cap 0.50]
"""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx2
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.llm import CallContext, EmbedResult, Gateway, PostgresLedger
from app.retrieval.search import RetrievalConfig, Retriever

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "eval"
SETS = {
    "page": ROOT / "spikes" / "retrieval_questions.json",
    "scenario": ROOT / "spikes" / "retrieval_questions_scenario.json",
}
CTX = CallContext(kind="eval_retrieval")

# Defaults (RetrievalConfig) are the shipped settings: ts_rank OR, ef_search 400, no demotion, rerank on.
VARIANTS: dict[str, RetrievalConfig] = {
    "fts[or_rank]": RetrievalConfig(fts_mode="or_rank", rerank=False),
    "fts[or_rank_cd]": RetrievalConfig(fts_mode="or_rank_cd", rerank=False),
    "fts[and_then_or]": RetrievalConfig(fts_mode="and_then_or", rerank=False),
    "vector": RetrievalConfig(rerank=False),  # handled specially: vector list only
    "hybrid[or_rank]": RetrievalConfig(fts_mode="or_rank", rerank=False),
    "hybrid[or_rank_cd]": RetrievalConfig(fts_mode="or_rank_cd", rerank=False),
    "hybrid[and_then_or]": RetrievalConfig(fts_mode="and_then_or", rerank=False),
    "hybrid[or_rank], demotion 0.5": RetrievalConfig(fts_mode="or_rank", rerank=False, superseded_factor=0.5),
    "hybrid[or_rank]+rerank": RetrievalConfig(fts_mode="or_rank"),
    "hybrid[or_rank]+rerank, demotion 0.5": RetrievalConfig(fts_mode="or_rank", superseded_factor=0.5),
    "hybrid[and_then_or]+rerank": RetrievalConfig(fts_mode="and_then_or"),
}


@dataclass
class CachingGateway:
    """Wraps the real gateway: cached embed/rerank, a spend cap on uncached calls, live latencies kept."""

    inner: Gateway
    cap: float
    path: Path
    spent: float = 0.0
    live_ms: dict[str, list[float]] = field(default_factory=lambda: {"embed": [], "rerank": []})
    store: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path.exists():
            self.store = json.loads(self.path.read_text())

    def save(self) -> None:
        self.path.write_text(json.dumps(self.store))

    def _check(self) -> None:
        if self.spent >= self.cap:
            raise RuntimeError(f"eval cap ${self.cap:.2f} reached")

    async def embed(self, ctx: CallContext, texts: list[str], *, model: str | None = None) -> EmbedResult:
        key = "e:" + hashlib.sha256(f"{model}|{texts[0]}".encode()).hexdigest()
        if key not in self.store:
            self._check()
            t = time.perf_counter()
            r = await self.inner.embed(ctx, texts, model=model)
            self.live_ms["embed"].append((time.perf_counter() - t) * 1000)
            self.spent += r.cost
            self.store[key] = r.vectors[0]
            self.save()
        return EmbedResult([self.store[key]], 0.0)

    async def rerank(self, ctx: CallContext, query: str, documents: list[str], *, top_n: int | None = None,
                     timeout_s: float = 1.5) -> list[tuple[int, float]]:  # fmt: skip
        key = "r:" + hashlib.sha256(json.dumps([query, sorted(documents)]).encode()).hexdigest()
        if key not in self.store:
            self._check()
            t = time.perf_counter()
            # Ask for every document's score once; any top_n can then be served from the cache.
            # Timeout is generous here: the eval measures quality, the benchmark measures latency.
            ranked = await self.inner.rerank(ctx, query, sorted(documents), top_n=len(documents), timeout_s=10.0)
            self.live_ms["rerank"].append((time.perf_counter() - t) * 1000)
            self.spent += self.inner.s.rerank_price_per_search
            self.store[key] = [[sorted(documents)[i], s] for i, s in ranked]
            self.save()
        pos = {d: i for i, d in enumerate(documents)}
        return [(pos[d], s) for d, s in self.store[key]][: top_n or len(documents)]


def hit_rank(q: dict[str, Any], hits: list[tuple[str, int | None]], k: int) -> int | None:
    for rank, (doc_id, page) in enumerate(hits[:k], start=1):
        if doc_id == q["doc_id"] and (not q["pages"] or page in q["pages"]):
            return rank
    return None


def score(ranks: list[int | None]) -> tuple[float, float]:
    return sum(r is not None for r in ranks) / len(ranks), sum(1 / r for r in ranks if r) / len(ranks)


def pct(xs: list[float], p: float) -> float:
    return round(sorted(xs)[max(0, int(len(xs) * p) - 1)], 1) if xs else float("nan")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--cap", type=float, default=0.50)
    args = ap.parse_args()

    s = Settings()
    OUT.mkdir(parents=True, exist_ok=True)
    questions = [q | {"set": name} for name, path in SETS.items() for q in json.loads(path.read_text())]
    async with (
        AsyncConnectionPool(s.dsn("tools_ro"), min_size=1, max_size=2, open=False) as ro,
        AsyncConnectionPool(s.dsn("app_rw"), min_size=1, max_size=2, open=False) as rw,
        httpx2.AsyncClient() as http,
    ):
        gw = CachingGateway(Gateway(s, http, PostgresLedger(rw)), args.cap, OUT / "cache.json")
        retriever = Retriever(ro, cast(Gateway, gw))
        results: dict[str, Any] = {}
        for name in args.variants:
            cfg = VARIANTS[name]
            ranks, timings, degraded = [], [], []
            for q in questions:
                if name == "vector":
                    async with ro.connection() as conn:
                        model, _ = await retriever.active_model(conn)
                        vec = (await gw.embed(CTX, [q["question"]], model=model)).vectors[0]
                        ids = await retriever.vector(conn, vec, model, ["public"], args.k)
                        cur = await conn.execute(
                            "SELECT chunk_id, doc_id, page FROM tools_chunks WHERE chunk_id = ANY(%s)", (ids,)
                        )
                        rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
                    hits = [rows[i] for i in ids]
                elif name.startswith("fts"):
                    r = await Retriever(ro, None, cfg).search(q["question"], CTX, config=cfg)
                    hits, _ = [(h.doc_id, h.page) for h in r.hits], timings.append(r.timings_ms)
                else:
                    r = await retriever.search(q["question"], CTX, config=cfg)
                    hits = [(h.doc_id, h.page) for h in r.hits]
                    timings.append(r.timings_ms)
                    degraded += r.degraded
                ranks.append(hit_rank(q, hits, args.k))
            row: dict[str, Any] = {}
            for subset in ("page", "scenario", "all"):
                sub = [r for r, q in zip(ranks, questions, strict=True) if subset == "all" or q["set"] == subset]
                row[subset] = score(sub)
            row["misses"] = [q["question"][:70] for r, q in zip(ranks, questions, strict=True) if r is None]
            row["degraded"] = sorted(set(degraded))
            if timings:
                row["timings_ms"] = {stage: {"p50": pct([t[stage] for t in timings if stage in t], 0.5),
                                             "p95": pct([t[stage] for t in timings if stage in t], 0.95)}
                                     for stage in timings[0]}  # fmt: skip
            results[name] = row
            print(
                f"{name:38s} " + "  ".join(f"{row[s_][0]:.2f} / {row[s_][1]:.2f}" for s_ in ("page", "scenario", "all"))
            )
        live = {k: {"n": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95)} for k, v in gw.live_ms.items()}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        report = {"k": args.k, "n": len(questions), "results": results, "live_call_ms": live,
                  "uncached_spend_usd": round(gw.spent, 4)}  # fmt: skip
        (OUT / f"retrieval-{stamp}.json").write_text(json.dumps(report, indent=2))
        print(f"\nlive calls: {live}\nuncached spend: ${gw.spent:.4f}\nwrote data/eval/retrieval-{stamp}.json")


if __name__ == "__main__":
    asyncio.run(main())
