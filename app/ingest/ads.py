"""Airworthiness Directive sync (DESIGN §6.2). Metadata comes from the Federal Register API and text from
govinfo.gov (the same official text). federalregister.gov's own full-text URLs return a bot-check page
to scripts, so they're not used (SPIKE §4).

The sync is idempotent and resumable. It re-reads from the high-water mark minus 3 days, skips documents
whose text hash hasn't changed, commits each document in its own transaction, and advances the
high-water mark only past documents that were committed.
"""

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.ingest.ad_parse import PARSER_VERSION, ParsedAD, parse_ad, text_from_govinfo_html
from app.ingest.chunk import chunk_text
from app.ingest.common import JobCapReached, SyncReport, embed_chunks
from app.ingest.fetch import FetchFailed, FetchRefused, SafeFetcher
from app.ingest.store import DocumentRow, bump_corpus_version, quarantine, stored_sha, upsert_document
from app.llm import Gateway

logger = logging.getLogger(__name__)

FR_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_FIELDS = ("document_number", "title", "publication_date", "effective_on", "pdf_url", "html_url")
SOURCE = "ads"
OVERLAP_DAYS = 3
MIN_TEXT, MAX_TEXT = 1_000, 2_000_000


def govinfo_text_url(pdf_url: str) -> str:
    """https://www.govinfo.gov/content/pkg/FR-2026-10-01/pdf/2026-20098.pdf -> .../html/2026-20098.htm"""
    return pdf_url.replace("/pdf/", "/html/").removesuffix(".pdf") + ".htm"


def invalid_reason(page: str, text: str) -> str | None:
    """A 200 can still be a block page: 235 of them were once stored as ADs (SPIKE §4)."""
    if "Request Access" in page:
        return "block page"
    if "Airworthiness Directive" not in text:
        return "no 'Airworthiness Directive' in text"
    if not MIN_TEXT <= len(text) <= MAX_TEXT:
        return f"text length {len(text)} outside [{MIN_TEXT}, {MAX_TEXT}]"
    return None


def ad_prefix(p: ParsedAD) -> str:
    models = ", ".join(p.models[:6])
    return f"[AD {p.ad_number}: {p.make}{' ' + models if models else ''}] "


async def list_fr_ads(fetcher: SafeFetcher, since: date, until: date) -> AsyncIterator[dict[str, Any]]:
    """FAA final rules titled 'Airworthiness Directives', oldest first, one query per calendar year
    (keeps each result set well under the API's pagination limits)."""
    for year in range(since.year, until.year + 1):
        lo, hi = max(since, date(year, 1, 1)), min(until, date(year, 12, 31))
        page = 1
        while True:
            q = [("per_page", "100"), ("page", str(page)), ("order", "oldest"),
                 ("conditions[agencies][]", "federal-aviation-administration"),
                 ("conditions[type][]", "RULE"),
                 ("conditions[term]", '"airworthiness directives"'),
                 ("conditions[publication_date][gte]", lo.isoformat()),
                 ("conditions[publication_date][lte]", hi.isoformat())]  # fmt: skip
            q += [("fields[]", f) for f in FR_FIELDS]
            got = await fetcher.get(f"{FR_API}?{urlencode(q)}")
            res = json.loads(got.body)
            for doc in res.get("results") or []:
                if str(doc.get("title", "")).startswith("Airworthiness Directives"):
                    yield doc
            if page >= int(res.get("total_pages") or 1):
                break
            page += 1


async def sync_ads(
    settings: Settings,
    fetcher: SafeFetcher,
    pool: AsyncConnectionPool,
    gateway: Gateway | None,
    *,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
) -> SyncReport:
    """`gateway=None` stores chunks without vectors (FTS only). `embed-missing` fills them in later."""
    report = SyncReport()
    until = until or date.today()
    async with pool.connection() as conn:
        cur = await conn.execute("INSERT INTO ingest_runs (source) VALUES (%s) RETURNING id", (SOURCE,))
        report.run_id = int((await cur.fetchone() or [0])[0])
        cur = await conn.execute("SELECT high_water_mark FROM sync_state WHERE source = %s", (SOURCE,))
        row = await cur.fetchone()
    start = since or (row[0] - timedelta(days=OVERLAP_DAYS) if row else settings.ad_backfill_from)
    committed_through: date | None = None
    first_failure: date | None = None
    changed = False
    crash: Exception | None = None

    try:
        async for fr in list_fr_ads(fetcher, start, until):
            if limit is not None and report.fetched >= limit:
                break
            report.fetched += 1
            pub = date.fromisoformat(fr["publication_date"])
            doc_id = f"ad:{fr['document_number']}"
            url = govinfo_text_url(fr["pdf_url"])
            try:
                got = await fetcher.get(url)
            except (FetchFailed, FetchRefused) as exc:
                report.failed += 1
                report.errors.append(f"{doc_id}: {exc}")
                first_failure = min(first_failure or pub, pub)
                continue
            page = got.body.decode("utf-8", errors="replace")
            text = text_from_govinfo_html(page)
            sha = hashlib.sha256(f"{PARSER_VERSION}\n{text}".encode()).hexdigest()
            reason = invalid_reason(page, text)
            parsed = None if reason else parse_ad(text, fr["title"], fr["document_number"])
            if reason or parsed is None:
                async with pool.connection() as conn:
                    await quarantine(conn, SOURCE, url, reason or "no AD number in header",
                                     hashlib.sha256(got.body).hexdigest())  # fmt: skip
                report.quarantined += 1
                committed_through = max(committed_through or pub, pub)
                continue
            async with pool.connection() as conn:
                if await stored_sha(conn, doc_id) == sha:
                    report.unchanged += 1
                    committed_through = max(committed_through or pub, pub)
                    continue
            chunks = chunk_text(doc_id, text, prefix=ad_prefix(parsed))
            vectors = await embed_chunks(gateway, chunks, report, settings.ingest_job_cap_usd) if gateway else None
            doc = DocumentRow(
                doc_id=doc_id, source="ad", ingest_origin="official", title=fr["title"], sha256=sha,
                access_level="public", url=url, ad_number=parsed.ad_number, doc_kind=parsed.kind,
                publication_date=pub,
                effective_date=date.fromisoformat(fr["effective_on"]) if fr.get("effective_on") else None,
                supersedes=parsed.supersedes, make=[parsed.make], models=parsed.models, jasc=parsed.jasc,
                applicability=parsed.applicability,
            )  # fmt: skip
            try:
                async with pool.connection() as conn:
                    outcome = await upsert_document(conn, doc, chunks, vectors, settings.embed_model)
            except psycopg.Error as exc:  # one bad document must not lose the run's other work
                report.failed += 1
                report.errors.append(f"{doc_id}: {type(exc).__name__}: {exc}")
                first_failure = min(first_failure or pub, pub)
                continue
            setattr(report, outcome, getattr(report, outcome) + 1)
            changed = True
            committed_through = max(committed_through or pub, pub)
    except JobCapReached as exc:
        report.stopped_early = str(exc)
    except (FetchFailed, FetchRefused) as exc:  # the listing itself failed
        report.stopped_early = f"listing failed: {exc}"
    except Exception as exc:  # anything else: close the run record as failed, then re-raise for the queue
        report.stopped_early = f"crashed: {type(exc).__name__}: {exc}"
        crash = exc

    # Advance the high-water mark only past what was committed, and never past a failed document.
    mark = committed_through
    if first_failure and (mark is None or first_failure <= mark):
        mark = first_failure - timedelta(days=1)
    ok = report.stopped_early is None and report.failed == 0
    async with pool.connection() as conn:
        async with conn.transaction():
            if changed:
                await bump_corpus_version(conn)
            if mark:
                # The scheduled sync (no `since`) sets the mark exactly, so a failed document is re-read next
                # time even if that moves the mark back. A one-off backfill with `since` only moves it forward.
                new_mark = "%(mark)s" if since is None else "GREATEST(sync_state.high_water_mark, %(mark)s)"
                await conn.execute(
                    f"""INSERT INTO sync_state (source, high_water_mark) VALUES (%(src)s, %(mark)s)
                        ON CONFLICT (source) DO UPDATE SET high_water_mark = {new_mark}, updated_at = now()""",  # noqa: S608
                    {"src": SOURCE, "mark": mark},
                )
            await conn.execute(
                """UPDATE ingest_runs SET finished_at = now(), status = %s, fetched = %s, inserted = %s,
                          updated = %s, quarantined = %s, cost = %s, error = %s WHERE id = %s""",
                ("succeeded" if ok else "failed", report.fetched, report.inserted, report.updated,
                 report.quarantined, report.cost, report.stopped_early or "; ".join(report.errors[:20]) or None,
                 report.run_id),
            )  # fmt: skip
    logger.info("sync_ads: %s", report)
    if crash:
        raise crash
    return report
