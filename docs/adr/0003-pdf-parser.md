# ADR-0003: PDF text extraction with pypdfium2

Status: Accepted · 2026-10-29 · implemented in #7 (`app/ingest/pdf.py`, `pdf_worker.py`)

## Context
The FAA handbooks are large, image-heavy PDFs (92 to 215 MB, 2,875 pages in total). Ingestion needs page-level text so answers can cite page numbers.

## Options (measured in the spike unless marked)
| Option | Result | Licence |
| --- | --- | --- |
| pypdf 6.21 | **Fails**: `LimitReachedError: Reached maximum number of tokens per stream` on the FAA handbooks | BSD |
| **pypdfium2** | All 2,875 pages in **6.9 s**; 9 near-empty pages | Apache-2.0 / BSD-3 (PDFium) |
| PyMuPDF | Not tested | **AGPL-3.0**, a problem for a public repo that companies may deploy |
| OCR or layout models (e.g. docling) | Not tested; heavier | Varies |

## Decision
Use **pypdfium2** for text with page numbers. Detect near-empty pages (under 50 characters) and record them, since they may be scanned figures.

## Consequences
- Tables and figure text come through as flat text. Phase 3 evals show whether table questions suffer; if they do, evaluate a layout-aware parser for those pages only.
- No OCR. Scanned-only PDFs are out of scope (`SCOPE.md`).
- **Untrusted uploads (added after the security review):** PDFium is native code parsing attacker-controlled input. Admin uploads are parsed only in the worker, in a subprocess with CPU, memory and time limits. They first pass a magic-byte check and size and page caps. A crash or timeout quarantines the file instead of retrying it. PDFium CVEs are tracked through pypdfium2 releases.

## Implementation (issue #7)
- **Sandbox:** every PDF, official or uploaded, is parsed by `python -I -m app.ingest.pdf_worker` in its own process. Limits are set in the child before PDFium is imported: `RLIMIT_CPU` 300 s, `RLIMIT_AS` 2 GB (Linux only; macOS ignores it), `RLIMIT_FSIZE` 0, `RLIMIT_NOFILE` 64. The parent enforces a 600 s wall clock, a 300 MB size cap, the `%PDF-` magic bytes, ≤ 3,000 pages, and that the output matches the declared page count. Any failure means quarantine, never retry.
- **Measured on the four FAA PDFs:** 2,875 pages in 8.7 s through the sandbox; 9 near-empty pages, the same as the spike.
- **Page numbers:** the PDF index is kept for evals and deep links, and a printed `page_label` for citations. It comes from embedded PDF page labels (General and Powerplant, 100 %) or footer text (Airframe 96 %, AC 91 % of pages).
- **Learned:** CPython ignores SIGXFSZ, so the file-size limit makes writes fail with EFBIG rather than killing the child. Nothing reaches the disk either way (tested).
- Full details, tests and operations: `docs/ingestion.md` §5.
