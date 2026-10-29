"""PDF text extraction child process. Never import this from the app. It's run by app/ingest/pdf.py as
`python -I -m app.ingest.pdf_worker <path> <max_pages> <cpu_s> <mem_mb>`.

PDFium is native code parsing what may be attacker-supplied input (admin uploads; ADR-0003), so it runs in
its own process with OS limits set before the file is opened:
  RLIMIT_CPU    CPU seconds. Exceeding it kills the process with SIGXCPU.
  RLIMIT_AS     address space. Enforced on Linux (the container target). macOS ignores it, so local runs
                rely on the parent's wall-clock timeout.
  RLIMIT_FSIZE  0. The parser has no reason to write files. CPython ignores SIGXFSZ, so a write fails with
                EFBIG instead of killing the process; either way nothing reaches the disk (tested).
  RLIMIT_NOFILE 64. Python's start-up plus one PDF.
Output on stdout: one JSON line `{"pages": N}`, then one `{"page": i, "label": ..., "text": ...}` per page.
"""

import json
import resource
import sys


def _limit(kind: int, value: int) -> None:
    try:
        resource.setrlimit(kind, (value, value))
    except (ValueError, OSError):
        pass  # not supported on this platform (e.g. RLIMIT_AS on macOS); the parent's timeout still applies


def main() -> int:
    path, max_pages, cpu_s, mem_mb = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    _limit(resource.RLIMIT_CPU, cpu_s)
    _limit(resource.RLIMIT_AS, mem_mb * 1024 * 1024)
    _limit(resource.RLIMIT_FSIZE, 0)
    _limit(resource.RLIMIT_NOFILE, 64)

    import pypdfium2 as pdfium  # imported after the limits, so PDFium starts already constrained

    doc = pdfium.PdfDocument(path)
    n = len(doc)
    if n > max_pages:
        print(json.dumps({"error": f"{n} pages > limit {max_pages}"}))
        return 3
    out = sys.stdout
    out.write(json.dumps({"pages": n}) + "\n")
    for i in range(n):
        page = doc[i]
        text = page.get_textpage().get_text_range()
        out.write(json.dumps({"page": i + 1, "label": doc.get_page_label(i) or None, "text": text}) + "\n")
        page.close()
    doc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
