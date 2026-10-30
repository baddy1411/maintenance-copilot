"""Hybrid retrieval on real Postgres (migrated to head, so the partial HNSW index exists). Needs Docker.

Data is seeded directly as `migrator` with deterministic vectors, so each test controls exactly what
full-text and vector search should find. The gateway is a stub that records what would leave the system.
"""

import math
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.ingest.store import halfvec
from app.llm import CallContext, EmbedResult, Gateway
from app.retrieval.search import RetrievalConfig, Retriever, rrf

pytestmark = pytest.mark.integration
MODEL = "voyageai/voyage-4-lite"
CTX = CallContext(kind="retrieval_test")


def unit(i: int, j: int | None = None) -> list[float]:
    """A 1024-d unit vector along axis i (or between axes i and j). Cosine order is then predictable."""
    v = [0.0] * 1024
    if j is None:
        v[i] = 1.0
    else:
        v[i] = v[j] = 1 / math.sqrt(2)
    return v


@dataclass
class StubGateway:
    query_vector: list[float] = field(default_factory=lambda: unit(0))
    embed_error: Exception | None = None
    rerank_error: Exception | None = None
    reranked: list[list[str]] = field(default_factory=list)  # documents sent to the reranker, per call

    async def embed(self, ctx: CallContext, texts: list[str], *, model: str | None = None) -> EmbedResult:
        if self.embed_error:
            raise self.embed_error
        return EmbedResult([self.query_vector for _ in texts], 0.0)

    async def rerank(self, ctx: CallContext, query: str, documents: list[str], *, top_n: int | None = None,
                     timeout_s: float = 1.5) -> list[tuple[int, float]]:  # fmt: skip
        self.reranked.append(list(documents))
        if self.rerank_error:
            raise self.rerank_error
        # Reverse the order it was given, so tests can tell the reranker's order from RRF's.
        order = list(reversed(range(len(documents))))[: top_n or len(documents)]
        return [(i, 1.0 - n / 10) for n, i in enumerate(order)]


DOCS = [  # (doc_id, source, access, ad_number, superseded_by, text, vector)
    ("hb", "handbook", "public", None, None, "Chip detector with metal particles in the engine oil.", unit(0)),
    ("ad:old", "ad", "public", "2020-01-01", "2024-01-01", "Hydraulic pump inspection of the brake system.", unit(5)),
    ("ad:new", "ad", "public", "2024-01-01", None, "Hydraulic pump inspection of the brake system.", unit(5)),
    ("upload:x", "upload", "restricted", None, None, "Chip detector secret shop procedure for oil.", unit(0, 1)),
    ("ac", "ac", "public", None, None, "Safety wire twisting and lockwire for turnbuckles.", unit(9)),
]


@pytest.fixture
def seeded(db: Settings) -> Iterator[None]:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE documents, chunks, chunk_embeddings CASCADE")
        for doc_id, source, access, ad, sup, text, vec in DOCS:
            conn.execute(
                "INSERT INTO documents (doc_id, source, ingest_origin, title, ad_number, superseded_by, sha256, "
                "access_level) VALUES (%s, %s, %s, %s, %s, %s, 'x', %s)",
                (doc_id, source, "admin_upload" if source == "upload" else "official", doc_id, ad, sup, access),
            )
            conn.execute("INSERT INTO chunks (chunk_id, doc_id, ord, text) VALUES (%s, %s, 0, %s)",
                         (f"{doc_id}:c0", doc_id, text))  # fmt: skip
            conn.execute(
                "INSERT INTO chunk_embeddings VALUES (%s, %s, %s::halfvec)", (f"{doc_id}:c0", MODEL, halfvec(vec))
            )
    yield


@pytest.fixture
async def pool(db: Settings, seeded: None) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(db.dsn("tools_ro"), min_size=1, max_size=2, open=False) as p:
        yield p


def retriever(pool: AsyncConnectionPool, gw: StubGateway, **cfg: Any) -> Retriever:
    return Retriever(pool, cast(Gateway, gw), RetrievalConfig(**cfg))


def test_rrf_sums_reciprocal_ranks() -> None:
    s = rrf([["a", "b"], ["b", "c"]], k=60)
    assert s["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert s["a"] == pytest.approx(1 / 61) and s["c"] == pytest.approx(1 / 62)
    assert sorted(s, key=lambda x: -s[x]) == ["b", "a", "c"]


async def test_hybrid_fuses_fulltext_and_vector_hits(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(query_vector=unit(9))  # vector points at the AC chunk, words point at the handbook
    r = await retriever(pool, gw, rerank=False).search("chip detector metal particles", CTX)
    by_id = {h.chunk_id: h for h in r.hits}
    assert by_id["hb:c0"].fts_rank == 1
    assert by_id["ac:c0"].vec_rank == 1 and by_id["ac:c0"].fts_rank is None
    assert r.degraded == [] and r.embed_model == MODEL
    assert set(r.timings_ms) >= {"fts_and_embed", "vector", "fetch", "total"}


async def test_restricted_chunks_never_returned_or_sent_to_the_reranker(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(query_vector=unit(0, 1))  # the restricted upload is the exact vector match
    r = await retriever(pool, gw).search("chip detector oil", CTX, levels=("public",))
    assert "upload:x:c0" not in {h.chunk_id for h in r.hits}
    assert not any("secret shop" in d for d in gw.reranked[0])
    r2 = await retriever(pool, gw).search("chip detector oil", CTX, levels=("public", "restricted"))
    assert "upload:x:c0" in {h.chunk_id for h in r2.hits}


async def test_superseded_ad_is_demoted_below_its_replacement(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(query_vector=unit(5))  # both ADs have identical text and vectors
    r = await retriever(pool, gw, rerank=False).search("hydraulic pump brake inspection", CTX)
    ads = [h.ad_number for h in r.hits if h.ad_number]
    assert ads == ["2024-01-01", "2020-01-01"]
    old = next(h for h in r.hits if h.ad_number == "2020-01-01")
    assert old.superseded_by == "2024-01-01"  # demoted, not hidden: someone may ask about it by number


async def test_reranker_order_is_used(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(query_vector=unit(0))
    plain = await retriever(pool, gw, rerank=False, k=3).search("chip detector oil", CTX)
    ranked = await retriever(pool, gw, k=3).search("chip detector oil", CTX)
    assert ranked.hits[0].rerank_score is not None
    assert [h.chunk_id for h in ranked.hits] != [h.chunk_id for h in plain.hits]


async def test_rerank_failure_falls_back_to_rrf_order(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(rerank_error=TimeoutError("rerank took 1.5 s"))
    r = await retriever(pool, gw).search("chip detector oil", CTX)
    plain = await retriever(pool, StubGateway(), rerank=False).search("chip detector oil", CTX)
    assert r.degraded == ["rerank_unavailable"]
    assert [h.chunk_id for h in r.hits] == [h.chunk_id for h in plain.hits]


async def test_embedding_failure_falls_back_to_fulltext(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(embed_error=ConnectionError("embeddings down"))
    r = await retriever(pool, gw, rerank=False).search("safety wire turnbuckles", CTX)
    assert r.degraded == ["embedding_unavailable"]
    assert r.hits[0].chunk_id == "ac:c0" and all(h.vec_rank is None for h in r.hits)


async def test_missing_vector_index_degrades_to_fulltext(db: Settings, pool: AsyncConnectionPool) -> None:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("UPDATE retrieval_config SET active_embed_model = 'voyageai/voyage-9-unindexed'")
    try:
        gw = StubGateway()
        r = await retriever(pool, gw, rerank=False).search("chip detector", CTX)
        assert r.degraded == ["vector_index_missing"] and r.embed_model == "voyageai/voyage-9-unindexed"
        assert r.hits and all(h.vec_rank is None for h in r.hits)
    finally:
        with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
            conn.execute("UPDATE retrieval_config SET active_embed_model = %s", (MODEL,))


async def test_and_then_or_prefers_documents_with_every_term(pool: AsyncConnectionPool) -> None:
    gw = StubGateway(query_vector=unit(200))  # no vector signal
    r = await retriever(pool, gw, rerank=False, fts_mode="and_then_or").search("chip detector engine", CTX)
    assert r.hits[0].chunk_id == "hb:c0"  # the only chunk with all three terms


async def test_partial_index_is_usable_with_a_literal_model_name(db: Settings, seeded: None) -> None:
    """The partial index predicate must be provable from the query. A literal is; a bind parameter isn't."""
    q = halfvec(unit(0))
    with psycopg.connect(db.dsn("tools_ro")) as conn:
        conn.execute("SET enable_seqscan = off")
        # Utility statements (EXPLAIN EXECUTE) can't take bind parameters; q is our own constant vector.
        literal = "\n".join(r[0] for r in conn.execute(
            f"EXPLAIN SELECT chunk_id FROM tools_chunk_embeddings WHERE embed_model = '{MODEL}' "
            f"ORDER BY embedding <=> '{q}'::halfvec LIMIT 5").fetchall())  # fmt: skip
        conn.execute("PREPARE p(text, halfvec) AS SELECT chunk_id FROM tools_chunk_embeddings "
                     "WHERE embed_model = $1 ORDER BY embedding <=> $2 LIMIT 5")  # fmt: skip
        conn.execute("SET plan_cache_mode = force_generic_plan")
        generic = "\n".join(r[0] for r in conn.execute(f"EXPLAIN EXECUTE p('{MODEL}', '{q}')").fetchall())
    assert "chunk_embeddings_hnsw_voyageai_voyage_4_lite" in literal
    assert "chunk_embeddings_hnsw_voyageai_voyage_4_lite" not in generic


async def test_per_call_config_controls_every_stage(pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: search(config=...) once ignored config.fts_mode, and the eval caught it (issue #8).
    It records the mode the full-text stage receives: on this small data both modes return the same
    order, so comparing results alone passed even with the bug (checked by putting the bug back)."""
    seen: list[str] = []
    real_fts = Retriever.fts

    async def spy(self: Retriever, conn: Any, query: str, levels: Any, n: int, mode: Any) -> list[str]:
        seen.append(mode)
        return await real_fts(self, conn, query, levels, n, mode)

    monkeypatch.setattr(Retriever, "fts", spy)
    default = retriever(pool, StubGateway(), rerank=False, fts_mode="or_rank")
    await default.search("chip detector", CTX, config=RetrievalConfig(rerank=False, fts_mode="and_then_or"))
    await default.search("chip detector", CTX)
    assert seen == ["and_then_or", "or_rank"]


async def test_vector_query_applies_ef_search_in_its_transaction(pool: AsyncConnectionPool) -> None:
    """SET LOCAL only lasts inside a transaction. Pool connections open one implicitly, so the setting must be
    visible right after the vector query, in the same connection context, and gone after it."""
    r = retriever(pool, StubGateway())
    async with pool.connection() as conn:
        await r.vector(conn, unit(0), MODEL, ["public"], 5, ef_search=400)
        cur = await conn.execute("SHOW hnsw.ef_search")
        assert (await cur.fetchone()) == ("400",)
    async with pool.connection() as conn:
        cur = await conn.execute("SHOW hnsw.ef_search")
        assert (await cur.fetchone()) == ("100",)  # the role default: SET LOCAL didn't leak back to the pool
