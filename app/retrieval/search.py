"""Hybrid retrieval over Postgres (ADR-0002, DESIGN §6.1, §8). The full design and measurements are in
docs/retrieval.md.

    query ─┬─ full-text (tools_chunks.tsv, GIN) ─────────────┐
           └─ embed (active model) ─ vector (partial HNSW) ──┴─ RRF (k=60) ─ demote superseded ADs
                                                                   ─ rerank top 50 (1.5 s timeout) ─ top k

Every stage that can fail degrades instead of failing the request, and says so in `degraded`:
  embedding down          -> full-text only
  vector index missing    -> full-text only (logged as an error: an ops problem, not a user one)
  reranker down or slow   -> RRF order (0.85 recall@5 in the spike, vs 0.97 with rerank)
Access control is applied inside both SQL queries, before anything is sent to the reranker (DESIGN §7.2).
All queries run as `tools_ro` (read-only, 5 s statement timeout). The vector query sets hnsw.ef_search per
transaction from RetrievalConfig (400; the role default of 100 lost recall, see docs/retrieval.md).
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool

from app.ingest.store import halfvec
from app.llm import CallContext, Gateway
from app.retrieval.vector_index import check_model_name, index_name

logger = logging.getLogger(__name__)

FtsMode = Literal["or_rank", "or_rank_cd", "and_then_or"]
AccessLevel = Literal["public", "restricted"]


@dataclass(frozen=True)
class RetrievalConfig:
    k: int = 5
    candidates: int = 50  # per list, and the number sent to the reranker
    rrf_k: int = 60
    fts_mode: FtsMode = "or_rank"
    # HNSW candidate list. Measured on the 40 eval queries vs an exact scan (docs/retrieval.md):
    # 100 -> 0.951 recall@50 (worst query 0.60); 400 -> 0.996 (worst 0.92) for about +2 ms at p95.
    ef_search: int = 400
    # RRF score multiplier for chunks of superseded ADs. Off (1.0): on the 40 eval questions it never changed
    # recall and lowered MRR (0.64 vs 0.67), and with rerank it has no effect. Kept for Phase 3 to retest
    # on supersession-specific questions.
    superseded_factor: float = 1.0
    rerank: bool = True
    rerank_timeout_s: float = 1.5


DEFAULT_CONFIG = RetrievalConfig()
DEFAULT_CONFIG_EF = DEFAULT_CONFIG.ef_search


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    source: str
    title: str
    text: str
    page: int | None
    page_label: str | None
    ad_number: str | None
    superseded_by: str | None
    fts_rank: int | None = None  # 1-based position in each list, None if absent
    vec_rank: int | None = None
    rrf: float = 0.0
    rerank_score: float | None = None


@dataclass
class SearchResult:
    hits: list[Hit]
    embed_model: str
    degraded: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


def rrf(rankings: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal rank fusion: score = sum of 1 / (k + rank) over the lists an item appears in."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def _fts_sql(mode: FtsMode, operator: str) -> sql.Composed:
    rank = "ts_rank_cd(tsv, q)" if mode == "or_rank_cd" else "ts_rank(tsv, q)"
    # plainto_tsquery gives 'a' & 'b' & 'c'; swapping the operator gives the OR form without parsing input.
    return sql.SQL(
        "SELECT chunk_id FROM tools_chunks, "
        "CAST(replace(plainto_tsquery('english', %(q)s)::text, ' & ', {op}) AS tsquery) q "
        "WHERE tsv @@ q AND access_level = ANY(%(levels)s::access_level[]) "
        "ORDER BY {rank} DESC, chunk_id LIMIT %(n)s"
    ).format(op=sql.Literal(operator), rank=sql.SQL(rank))


class Retriever:
    def __init__(self, pool: AsyncConnectionPool, gateway: Gateway | None, config: RetrievalConfig = DEFAULT_CONFIG):
        self.pool = pool  # tools_ro
        self.gateway = gateway
        self.config = config
        self._model: tuple[str, bool, float] | None = None  # (active model, index exists, fetched at)

    async def active_model(self, conn: AsyncConnection[Any]) -> tuple[str, bool]:
        """The active embedding model (a database row, not env config: DESIGN §8.1) and whether its
        partial index exists. Cached for 60 s."""
        if self._model and time.monotonic() - self._model[2] < 60:
            return self._model[0], self._model[1]
        cur = await conn.execute("SELECT active_embed_model FROM tools_retrieval_config")
        row = await cur.fetchone()
        model = check_model_name(str(row[0])) if row else ""
        cur = await conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (index_name(model),))
        has_index = await cur.fetchone() is not None
        self._model = (model, has_index, time.monotonic())
        return model, has_index

    async def fts(self, conn: AsyncConnection[Any], query: str, levels: Sequence[str], n: int,
                  mode: FtsMode) -> list[str]:  # fmt: skip
        params = {"q": query, "levels": list(levels), "n": n}
        if mode == "and_then_or":
            cur = await conn.execute(_fts_sql(mode, " & "), params)
            ids = [r[0] for r in await cur.fetchall()]
            if len(ids) >= n:
                return ids
            cur = await conn.execute(_fts_sql(mode, " | "), params)
            seen = set(ids)
            return ids + [r[0] for r in await cur.fetchall() if r[0] not in seen][: n - len(ids)]
        cur = await conn.execute(_fts_sql(mode, " | "), params)
        return [r[0] for r in await cur.fetchall()]

    async def vector(self, conn: AsyncConnection[Any], vec: list[float], model: str, levels: Sequence[str],
                     n: int, ef_search: int = DEFAULT_CONFIG_EF) -> list[str]:  # fmt: skip
        # The model is a literal so the planner can use its partial index (vector_index.py).
        q = sql.SQL(
            "SELECT e.chunk_id FROM tools_chunk_embeddings e JOIN tools_chunks c USING (chunk_id) "
            "WHERE e.embed_model = {model} AND c.access_level = ANY(%s::access_level[]) "
            "ORDER BY e.embedding <=> %s::halfvec LIMIT %s"
        ).format(model=sql.Literal(check_model_name(model)))
        await conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(int(ef_search))))
        cur = await conn.execute(q, (list(levels), halfvec(vec), n))
        return [r[0] for r in await cur.fetchall()]

    async def _rows(self, conn: AsyncConnection[Any], ids: list[str]) -> dict[str, Hit]:
        cur = await conn.execute(
            "SELECT chunk_id, doc_id, source, title, text, page, page_label, ad_number, superseded_by "
            "FROM tools_chunks WHERE chunk_id = ANY(%s)",
            (ids,),
        )
        return {r[0]: Hit(*r) for r in await cur.fetchall()}

    async def search(self, query: str, ctx: CallContext, levels: Sequence[AccessLevel] = ("public",),
                     config: RetrievalConfig | None = None) -> SearchResult:  # fmt: skip
        cfg = config or self.config
        timings: dict[str, float] = {}
        degraded: list[str] = []
        t0 = time.perf_counter()

        def mark(stage: str, since: float) -> float:
            now = time.perf_counter()
            timings[stage] = round((now - since) * 1000, 1)
            return now

        async with self.pool.connection() as conn:
            model, has_index = await self.active_model(conn)
            if not has_index:
                logger.error("no vector index for active model %s: full-text only", model)
                degraded.append("vector_index_missing")

            async def embed() -> list[float] | None:
                if self.gateway is None or not has_index:
                    return None
                t = time.perf_counter()
                try:
                    return (await self.gateway.embed(ctx, [query], model=model)).vectors[0]
                except Exception as exc:  # any failure here means full-text only, never a failed request
                    logger.warning("query embedding failed: %s", exc)
                    degraded.append("embedding_unavailable")
                    return None
                finally:
                    mark("embed", t)

            # The embedding call and the full-text query overlap (DESIGN §6.1).
            t = time.perf_counter()
            fts_ids, vec = await asyncio.gather(self.fts(conn, query, levels, cfg.candidates, cfg.fts_mode), embed())
            t = mark("fts_and_embed", t)
            vec_ids = []
            if vec is not None:
                vec_ids = await self.vector(conn, vec, model, levels, cfg.candidates, cfg.ef_search)
            t = mark("vector", t)

            scores = rrf([fts_ids, vec_ids], cfg.rrf_k)
            rows = await self._rows(conn, list(scores))
            t = mark("fetch", t)

        fts_pos = {c: i for i, c in enumerate(fts_ids, start=1)}
        vec_pos = {c: i for i, c in enumerate(vec_ids, start=1)}
        hits = []
        for cid, score in scores.items():
            h = rows[cid]
            factor = cfg.superseded_factor if h.superseded_by else 1.0
            hits.append(replace(h, fts_rank=fts_pos.get(cid), vec_rank=vec_pos.get(cid), rrf=score * factor))
        hits.sort(key=lambda h: (-h.rrf, h.chunk_id))
        hits = hits[: cfg.candidates]

        if cfg.rerank and self.gateway is not None and hits:
            try:
                ranked = await self.gateway.rerank(ctx, query, [h.text for h in hits], top_n=cfg.k,
                                                   timeout_s=cfg.rerank_timeout_s)  # fmt: skip
                reranked = []
                for idx, s in ranked:
                    hits[idx].rerank_score = s
                    reranked.append(hits[idx])
                hits = reranked
            except Exception as exc:  # timeout, HTTP error, spend cap: all fall back to RRF order
                logger.warning("rerank unavailable, using RRF order: %s", exc)
                degraded.append("rerank_unavailable")
            mark("rerank", t)
        timings["total"] = round((time.perf_counter() - t0) * 1000, 1)
        return SearchResult(hits=hits[: cfg.k], embed_model=model, degraded=degraded, timings_ms=timings)
