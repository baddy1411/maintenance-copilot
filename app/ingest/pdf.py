"""Sandboxed PDF text extraction (ADR-0003, DESIGN §6.2). The parent side: validates the file, runs
app/ingest/pdf_worker.py in a separate process, and turns any crash, limit or timeout into PdfRejected.
That error means quarantine, never retry.
"""

import asyncio
import json
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from app.ingest.text import clean

MAGIC = b"%PDF-"


@dataclass(frozen=True)
class PdfLimits:
    max_bytes: int = 300_000_000  # the largest FAA handbook is 215 MB
    max_pages: int = 3_000
    cpu_s: int = 300
    mem_mb: int = 2_048
    wall_s: float = 600.0


DEFAULT_LIMITS = PdfLimits()


@dataclass(frozen=True)
class Page:
    page: int  # 1-based PDF page index: what evals and #page=N deep links use
    label: str | None  # printed page number for citations ("8-6", "Page 8-22")
    text: str


class PdfRejected(RuntimeError):
    pass


# Printed page numbers when the PDF has no page labels (measured on the FAA files, see docs/ingestion.md):
#   Airframe handbook: "8-6" as the last token of the page.
#   AC 43.13-1B: "Page 8-22 Par 8-37" in the footer.
FOOTER_PAGE = re.compile(r"\bPage\s+(\d{1,2}-\d{1,3})\b")
TRAILING_PAGE = re.compile(r"(?:^|\s)(\d{1,2}-\d{1,3}|[A-Z]-\d{1,3})\s*$")


def printed_label(embedded: str | None, text: str) -> str | None:
    if embedded:
        return embedded
    if m := FOOTER_PAGE.search(text[-200:]):
        return m.group(1)
    if m := TRAILING_PAGE.search(text):
        return m.group(1)
    return None


def check_file(path: Path, limits: PdfLimits) -> None:
    size = path.stat().st_size
    if size > limits.max_bytes:
        raise PdfRejected(f"{size:,} bytes > limit {limits.max_bytes:,}")
    with path.open("rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise PdfRejected("not a PDF (magic bytes)")


async def extract_pages(
    path: Path, limits: PdfLimits = DEFAULT_LIMITS, *, worker: list[str] | None = None
) -> list[Page]:
    """`worker` overrides the child command, for tests that simulate a hang or a crash."""
    check_file(path, limits)
    cmd = worker or [sys.executable, "-I", "-m", "app.ingest.pdf_worker"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, str(path), str(limits.max_pages), str(limits.cpu_s), str(limits.mem_mb),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limits.wall_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise PdfRejected(f"parser timed out after {limits.wall_s:.0f}s") from None
    if proc.returncode != 0:
        rc = proc.returncode or 0
        if rc < 0:
            reason = "CPU limit" if -rc == signal.SIGXCPU else f"killed by signal {signal.Signals(-rc).name}"
        else:
            last = (out.decode(errors="replace").strip().splitlines() or [""])[-1]
            detail = json.loads(last).get("error") if last.startswith("{") else None
            reason = detail or f"exit {rc}: {err.decode(errors='replace').strip()[-300:]}"
        raise PdfRejected(f"parser failed: {reason}")

    lines = out.decode().splitlines()
    try:
        expected = int(json.loads(lines[0])["pages"])
        records = [json.loads(line) for line in lines[1:]]
    except (IndexError, KeyError, ValueError) as exc:
        raise PdfRejected(f"parser output unreadable: {exc}") from None
    if len(records) != expected:
        raise PdfRejected(f"parser returned {len(records)} of {expected} pages")
    pages = []
    for r in records:
        text = clean(r["text"])
        pages.append(Page(page=int(r["page"]), label=printed_label(r.get("label"), text), text=text))
    return pages
