"""PDF documents: the official FAA handbooks and AC 43.13-1B, and admin uploads (DESIGN §6.2, ADR-0003).

Both paths share one pipeline:
  file -> size and magic-byte check -> sandboxed text extraction (app/ingest/pdf.py)
       -> one chunk set per page -> embed (job cap) -> one transaction per document.
They differ in trust and visibility:
  official: fetched from faa.gov through the allow-listed fetcher, `public`, origin `official`.
  upload:   a local file from an admin, stored under its sha256, `restricted` by default, origin
            `admin_upload`. It's never parsed in the API process. This runs in the worker or CLI.

Design decisions (details and measurements in docs/ingestion.md):
- No chunk prefix for handbooks. The spike's retrieval numbers (0.97 recall@5) were measured without one,
  and #8 must compare like with like. A "[title, p. 8-6]" prefix is a Phase 3 experiment.
- The content hash covers PDF_PARSER_VERSION + file bytes, so a parser change re-processes stored PDFs
  (same rule as ADs, DESIGN P2-10).
"""

import asyncio
import dataclasses
import hashlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.ingest.chunk import Chunk, chunk_text
from app.ingest.common import JobCapReached, SyncReport, embed_chunks
from app.ingest.fetch import FetchFailed, FetchRefused, SafeFetcher
from app.ingest.pdf import Page, PdfLimits, PdfRejected, check_file, extract_pages
from app.ingest.store import DocumentRow, bump_corpus_version, quarantine, stored_sha, upsert_document
from app.llm import Gateway

logger = logging.getLogger(__name__)

PDF_PARSER_VERSION = "2"  # 2: hyphenation marks (U+FFFE, U+00AD) removed; see app/ingest/text.py
NEAR_EMPTY_CHARS = 50  # scanned figure pages; 9 of 2,875 in the FAA set


@dataclass(frozen=True)
class OfficialPdf:
    doc_id: str
    source: Literal["handbook", "ac"]
    title: str
    url: str
    published: date | None = None


# Titles are from each PDF's own metadata and cover page (checked 2026-10-29). doc_ids match the spike's
# question labels (spikes/retrieval_questions*.json).
OFFICIAL_PDFS = (
    OfficialPdf("faa-amt-general", "handbook", "Aviation Maintenance Technician Handbook - General (FAA-H-8083-30B)",
                "https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/amtg_handbook.pdf"),
    OfficialPdf("faa-amt-airframe", "handbook",
                "Aviation Maintenance Technician Handbook - Airframe (FAA-H-8083-31B)",
                "https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/"
                "FAA-H-8083-31B_Aviation_Maintenance_Technician_Handbook.pdf"),
    OfficialPdf("faa-amt-powerplant", "handbook",
                "Aviation Maintenance Technician Handbook - Powerplant (FAA-H-8083-32B)",
                "https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/amt_powerplant_handbook.pdf"),
    OfficialPdf("faa-ac-43-13-1b", "ac",
                "AC 43.13-1B Acceptable Methods, Techniques, and Practices - Aircraft Inspection and Repair "
                "(with Change 1)",
                "https://www.faa.gov/documentLibrary/media/Advisory_Circular/AC_43.13-1B_w-chg1.pdf",
                date(1998, 9, 8)),
)  # fmt: skip


def limits(s: Settings) -> PdfLimits:
    return PdfLimits(s.pdf_max_bytes, s.pdf_max_pages, s.pdf_cpu_s, s.pdf_mem_mb, s.pdf_wall_s)


def content_sha(data: bytes) -> str:
    return hashlib.sha256(PDF_PARSER_VERSION.encode() + b"\n" + data).hexdigest()


def page_chunks(doc_id: str, pages: list[Page]) -> tuple[list[Chunk], int]:
    """Chunks never span pages, so every citation points at exactly one page (as in the spike).
    Returns the chunks and the number of near-empty pages skipped."""
    chunks: list[Chunk] = []
    empty = 0
    for p in pages:
        if len(p.text.strip()) < NEAR_EMPTY_CHARS:
            empty += 1
            continue
        chunks += chunk_text(doc_id, p.text, page=p.page, page_label=p.label, start_ord=len(chunks))
    return chunks, empty


async def _store(
    settings: Settings, pool: AsyncConnectionPool, gateway: Gateway | None, report: SyncReport,
    doc: DocumentRow, path: Path, source_tag: str, url_for_quarantine: str,
) -> bool:  # fmt: skip
    """Extract, chunk, embed and store one PDF. Returns True if the corpus changed."""
    try:
        pages = await extract_pages(path, limits(settings))
    except PdfRejected as exc:
        async with pool.connection() as conn:
            await quarantine(conn, source_tag, url_for_quarantine, str(exc), doc.sha256)
        report.quarantined += 1
        return False
    chunks, empty = page_chunks(doc.doc_id, pages)
    if not chunks:
        async with pool.connection() as conn:
            await quarantine(conn, source_tag, url_for_quarantine, "no extractable text (scanned PDF?)", doc.sha256)
        report.quarantined += 1
        return False
    vectors = await embed_chunks(gateway, chunks, report, settings.ingest_job_cap_usd) if gateway else None
    try:
        async with pool.connection() as conn:
            outcome = await upsert_document(conn, doc, chunks, vectors, settings.embed_model)
    except psycopg.Error as exc:
        report.failed += 1
        report.errors.append(f"{doc.doc_id}: {type(exc).__name__}: {exc}")
        return False
    setattr(report, outcome, getattr(report, outcome) + 1)
    logger.info("%s: %d pages, %d chunks, %d near-empty pages skipped", doc.doc_id, len(pages), len(chunks), empty)
    return True


async def _start_run(pool: AsyncConnectionPool, source: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("INSERT INTO ingest_runs (source) VALUES (%s) RETURNING id", (source,))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _finish_run(pool: AsyncConnectionPool, report: SyncReport, changed: bool) -> None:
    ok = report.stopped_early is None and report.failed == 0
    async with pool.connection() as conn, conn.transaction():
        if changed:
            await bump_corpus_version(conn)
        await conn.execute(
            """UPDATE ingest_runs SET finished_at = now(), status = %s, fetched = %s, inserted = %s, updated = %s,
                      quarantined = %s, cost = %s, error = %s WHERE id = %s""",
            ("succeeded" if ok else "failed", report.fetched, report.inserted, report.updated, report.quarantined,
             report.cost, report.stopped_early or "; ".join(report.errors[:20]) or None, report.run_id),
        )  # fmt: skip


async def ingest_official(
    settings: Settings, fetcher: SafeFetcher, pool: AsyncConnectionPool, gateway: Gateway | None,
    *, only: set[str] | None = None,
) -> SyncReport:  # fmt: skip
    """Fetch and (re)ingest the official PDFs. An unchanged file is skipped without parsing or spend."""
    report = SyncReport(run_id=await _start_run(pool, "handbooks"))
    big = dataclasses.replace(fetcher, max_bytes=settings.pdf_max_bytes, timeout_s=300.0)
    changed = False
    workdir = Path(tempfile.mkdtemp(prefix="copilot-pdf-"))  # downloads stay apart from code (untrusted)
    crash: Exception | None = None
    try:
        for spec in OFFICIAL_PDFS:
            if only and spec.doc_id not in only:
                continue
            report.fetched += 1
            try:
                got = await big.get(spec.url)
            except (FetchFailed, FetchRefused) as exc:
                report.failed += 1
                report.errors.append(f"{spec.doc_id}: {exc}")
                continue
            sha = content_sha(got.body)
            async with pool.connection() as conn:
                if await stored_sha(conn, spec.doc_id) == sha:
                    report.unchanged += 1
                    continue
            path = workdir / f"{hashlib.sha256(got.body).hexdigest()}.pdf"
            await asyncio.to_thread(path.write_bytes, got.body)  # up to 300 MB: keep it off the event loop
            doc = DocumentRow(doc_id=spec.doc_id, source=spec.source, ingest_origin="official", title=spec.title,
                              sha256=sha, access_level="public", url=spec.url,
                              publication_date=spec.published)  # fmt: skip
            changed |= await _store(settings, pool, gateway, report, doc, path, "handbooks", spec.url)
            path.unlink()
    except JobCapReached as exc:
        report.stopped_early = str(exc)
    except Exception as exc:  # close the run record, then re-raise for the queue
        report.stopped_early = f"crashed: {type(exc).__name__}: {exc}"
        crash = exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    await _finish_run(pool, report, changed)
    if crash:
        raise crash
    return report


async def ingest_upload(
    settings: Settings, pool: AsyncConnectionPool, gateway: Gateway | None, file: Path, title: str,
    *, access_level: Literal["public", "restricted"] = "restricted",
) -> tuple[SyncReport, str]:  # fmt: skip
    """Ingest an admin-supplied PDF. Returns the report and the doc_id (`upload:<sha256 prefix>`).
    The file is copied under its hash first, so the stored name never comes from the user. The API
    endpoint that accepts uploads (and writes the audit entry) comes with the admin API (#13)."""
    report = SyncReport(run_id=await _start_run(pool, "upload"), fetched=1)
    data = await asyncio.to_thread(file.read_bytes)
    raw = hashlib.sha256(data).hexdigest()
    doc_id = f"upload:{raw[:16]}"
    changed = False
    crash: Exception | None = None
    try:
        try:
            check_file(file, limits(settings))
        except PdfRejected as exc:
            async with pool.connection() as conn:
                await quarantine(conn, "upload", doc_id, str(exc), raw)
            report.quarantined += 1
        else:
            await asyncio.to_thread(settings.upload_dir.mkdir, parents=True, exist_ok=True)
            stored = settings.upload_dir / f"{raw}.pdf"
            if not await asyncio.to_thread(stored.exists):
                await asyncio.to_thread(stored.write_bytes, data)
            sha = content_sha(data)
            async with pool.connection() as conn:
                unchanged = await stored_sha(conn, doc_id) == sha
            if unchanged:
                report.unchanged += 1
            else:
                doc = DocumentRow(doc_id=doc_id, source="upload", ingest_origin="admin_upload", title=title,
                                  sha256=sha, access_level=access_level, url=None)  # fmt: skip
                changed = await _store(settings, pool, gateway, report, doc, stored, "upload", doc_id)
    except JobCapReached as exc:
        report.stopped_early = str(exc)
    except Exception as exc:
        report.stopped_early = f"crashed: {type(exc).__name__}: {exc}"
        crash = exc
    await _finish_run(pool, report, changed)
    if crash:
        raise crash
    return report, doc_id
