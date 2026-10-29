# ADR-0003: PDF text extraction with pypdfium2

Status: Proposed · 2026-10-29

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
