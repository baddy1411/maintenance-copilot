"""Writes to the corpus tables. One transaction per document: it becomes searchable whole or not at all."""

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from psycopg import AsyncConnection

from app.ingest.chunk import Chunk


@dataclass(frozen=True)
class DocumentRow:
    doc_id: str
    source: Literal["handbook", "ac", "ad", "upload"]
    ingest_origin: Literal["official", "admin_upload"]
    title: str
    sha256: str
    access_level: Literal["public", "restricted"]
    url: str | None = None
    ad_number: str | None = None
    doc_kind: Literal["original", "correction", "revision"] = "original"
    publication_date: date | None = None
    effective_date: date | None = None
    supersedes: list[str] = field(default_factory=list)
    make: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    jasc: list[str] = field(default_factory=list)
    applicability: str | None = None


def halfvec(v: list[float]) -> str:
    return json.dumps(v)  # '[0.1, 0.2, ...]' is pgvector's text input format


async def stored_sha(conn: AsyncConnection[Any], doc_id: str) -> str | None:
    cur = await conn.execute("SELECT sha256 FROM documents WHERE doc_id = %s", (doc_id,))
    row = await cur.fetchone()
    return str(row[0]) if row else None


async def upsert_document(
    conn: AsyncConnection[Any],
    doc: DocumentRow,
    chunks: list[Chunk],
    vectors: list[list[float]] | None,
    embed_model: str,
) -> Literal["inserted", "updated"]:
    """Replace the document and all its chunks (and their embeddings, by cascade) in one transaction,
    then update supersession in both directions:
    - older ADs this one replaces get `superseded_by` = this AD
    - this AD gets `superseded_by` if a newer, already-ingested AD replaces it (out-of-order backfill)."""
    if vectors is not None and len(vectors) != len(chunks):
        raise ValueError(f"{len(vectors)} vectors for {len(chunks)} chunks")
    async with conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO documents (doc_id, source, ingest_origin, title, ad_number, doc_kind, publication_date,
                                   effective_date, supersedes, make, models, jasc, applicability, url, sha256,
                                   access_level)
            VALUES (%(doc_id)s, %(source)s, %(ingest_origin)s, %(title)s, %(ad_number)s, %(doc_kind)s,
                    %(publication_date)s, %(effective_date)s, %(supersedes)s, %(make)s, %(models)s, %(jasc)s,
                    %(applicability)s, %(url)s, %(sha256)s, %(access_level)s)
            ON CONFLICT (doc_id) DO UPDATE SET
                title = EXCLUDED.title, ad_number = EXCLUDED.ad_number, doc_kind = EXCLUDED.doc_kind,
                publication_date = EXCLUDED.publication_date, effective_date = EXCLUDED.effective_date,
                supersedes = EXCLUDED.supersedes, make = EXCLUDED.make, models = EXCLUDED.models,
                jasc = EXCLUDED.jasc, applicability = EXCLUDED.applicability, url = EXCLUDED.url,
                sha256 = EXCLUDED.sha256, ingested_at = now()
            RETURNING (xmax = 0) AS inserted
            """,
            doc.__dict__,
        )
        row = await cur.fetchone()
        inserted = bool(row and row[0])
        await conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc.doc_id,))
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO chunks (chunk_id, doc_id, page, page_label, ord, text) VALUES (%s, %s, %s, %s, %s, %s)",
                [(ch.chunk_id, ch.doc_id, ch.page, ch.page_label, ch.ord, ch.text) for ch in chunks],
            )
            if vectors is not None:
                await c.executemany(
                    "INSERT INTO chunk_embeddings (chunk_id, embed_model, embedding) VALUES (%s, %s, %s::halfvec)",
                    [(ch.chunk_id, embed_model, halfvec(v)) for ch, v in zip(chunks, vectors, strict=True)],
                )
        if doc.source == "ad" and doc.ad_number:
            if doc.supersedes:
                await conn.execute(
                    "UPDATE documents SET superseded_by = %s WHERE source = 'ad' AND ad_number = ANY(%s) "
                    "AND superseded_by IS DISTINCT FROM %s",
                    (doc.ad_number, doc.supersedes, doc.ad_number),
                )
            await conn.execute(
                """
                UPDATE documents SET superseded_by = (
                    SELECT n.ad_number FROM documents n
                    WHERE n.source = 'ad' AND %(ad)s = ANY(n.supersedes)
                    ORDER BY n.publication_date DESC NULLS LAST LIMIT 1)
                WHERE source = 'ad' AND ad_number = %(ad)s
                """,
                {"ad": doc.ad_number},
            )
    return "inserted" if inserted else "updated"


async def quarantine(conn: AsyncConnection[Any], source: str, url: str, reason: str, body_sha256: str) -> None:
    await conn.execute(
        """
        INSERT INTO quarantine (source, url, reason, body_sha256) VALUES (%s, %s, %s, %s)
        ON CONFLICT (url, body_sha256) DO UPDATE
            SET last_seen = now(), attempts = quarantine.attempts + 1, reason = EXCLUDED.reason
        """,
        (source, url, reason, body_sha256),
    )


async def bump_corpus_version(conn: AsyncConnection[Any]) -> None:
    """Every cache key includes corpus_version, so a change in the corpus invalidates cached answers (§7.2)."""
    await conn.execute("UPDATE retrieval_config SET corpus_version = corpus_version + 1, updated_at = now()")
