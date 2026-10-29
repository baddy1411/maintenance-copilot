"""Embed every chunk that has no vector for the given model (default: the active one).

Used after an FTS-only sync (`--no-embed`), and as the backfill step of an embedder change (DESIGN §8.1).
Each batch of 64 commits on its own, so a stopped job pays again for at most one batch.
"""

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.ingest.store import halfvec
from app.llm import CallContext, Gateway

BATCH = 64


@dataclass
class EmbedReport:
    embedded: int = 0
    cost: float = 0.0
    stopped_early: str | None = None


async def embed_missing(
    pool: AsyncConnectionPool, gateway: Gateway, model: str, *, cap_usd: float, limit: int | None = None
) -> EmbedReport:
    report = EmbedReport()
    while limit is None or report.embedded < limit:
        if report.cost >= cap_usd:
            report.stopped_early = f"job cap ${cap_usd:.2f} reached"
            break
        take = BATCH if limit is None else min(BATCH, limit - report.embedded)
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT c.chunk_id, c.text FROM chunks c
                   WHERE NOT EXISTS (SELECT 1 FROM chunk_embeddings e
                                     WHERE e.chunk_id = c.chunk_id AND e.embed_model = %s)
                   ORDER BY c.chunk_id LIMIT %s""",
                (model, take),
            )
            rows = await cur.fetchall()
        if not rows:
            break
        r = await gateway.embed(CallContext(kind="embed_corpus"), [t for _, t in rows], model=model)
        report.cost += r.cost
        async with pool.connection() as conn, conn.transaction(), conn.cursor() as c:
            await c.executemany(
                "INSERT INTO chunk_embeddings (chunk_id, embed_model, embedding) VALUES (%s, %s, %s::halfvec) "
                "ON CONFLICT DO NOTHING",
                [(cid, model, halfvec(v)) for (cid, _), v in zip(rows, r.vectors, strict=True)],
            )
        report.embedded += len(rows)
    return report
