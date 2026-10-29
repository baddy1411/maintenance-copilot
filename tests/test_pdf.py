"""The PDF sandbox (ADR-0003): extraction, limits, and every way the child can fail."""

import sys
from pathlib import Path

import pytest

from app.ingest.pdf import PdfLimits, PdfRejected, extract_pages, printed_label
from tests.pdf_factory import handbook_page, make_pdf

SPIKE_AC = Path(__file__).parent.parent / "spikes" / "data" / "faa" / "faa-ac-43-13-1b.pdf"
FAST = PdfLimits(wall_s=20)


def write(tmp_path: Path, data: bytes, name: str = "doc.pdf") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


async def test_extracts_text_and_printed_page_numbers(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["Chapter 8 Painting", "Air line filter", "8-6"],
                                    ["Footer style", "Page 8-22 Par 8-37"], []]))  # fmt: skip
    pages = await extract_pages(pdf, FAST)
    assert [p.page for p in pages] == [1, 2, 3]
    assert "Air line filter" in pages[0].text
    assert [p.label for p in pages] == ["8-6", "8-22", None]


@pytest.mark.parametrize(
    ("embedded", "text", "label"),
    [("10-25", "anything 8-6", "10-25"), (None, "spray guns Figure 8-3. 8-6 ", "8-6"),
     (None, "fluid leaks. 8-39. [RESERVED.] Page 8-22 Par 8-37", "8-22"), (None, "no number here", None),
     (None, "Index I-11", "I-11")],
)  # fmt: skip
def test_printed_label(embedded: str | None, text: str, label: str | None) -> None:
    assert printed_label(embedded, text) == label


async def test_rejects_non_pdf_by_magic_bytes(tmp_path: Path) -> None:
    with pytest.raises(PdfRejected, match="magic bytes"):
        await extract_pages(write(tmp_path, b"<html>not a pdf</html>"), FAST)


async def test_rejects_oversized_file_before_parsing(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["x" * 50]]))
    with pytest.raises(PdfRejected, match="bytes > limit"):
        await extract_pages(pdf, PdfLimits(max_bytes=100))


async def test_rejects_too_many_pages(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["page"]] * 5))
    with pytest.raises(PdfRejected, match="5 pages > limit 2"):
        await extract_pages(pdf, PdfLimits(max_pages=2, wall_s=20))


async def test_corrupt_pdf_is_rejected_not_raised(tmp_path: Path) -> None:
    with pytest.raises(PdfRejected, match="parser failed: exit 1"):
        await extract_pages(write(tmp_path, b"%PDF-1.4\n" + b"\x00garbage" * 200), FAST)


async def test_hung_parser_is_killed_at_the_wall_clock_limit(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["x"]]))
    with pytest.raises(PdfRejected, match="timed out after 1s"):
        await extract_pages(pdf, PdfLimits(wall_s=1), worker=[sys.executable, "-c", "import time; time.sleep(30)"])


async def test_crashing_parser_is_rejected(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["x"]]))
    with pytest.raises(PdfRejected, match="killed by signal SIGSEGV"):
        await extract_pages(pdf, FAST, worker=[sys.executable, "-c",
                                               "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)"])  # fmt: skip


async def test_cpu_limit_kills_a_spinning_parser(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["x"]]))
    spin = "import resource; resource.setrlimit(resource.RLIMIT_CPU, (1, 1))\nwhile True: pass"
    with pytest.raises(PdfRejected, match="CPU limit"):
        await extract_pages(pdf, FAST, worker=[sys.executable, "-c", spin])


def test_worker_limits_block_file_writes(tmp_path: Path) -> None:
    """The worker's own _limit() with RLIMIT_FSIZE 0. CPython ignores SIGXFSZ, so the write fails with
    EFBIG (errno 27) instead of killing the process. Either way, nothing reaches the disk."""
    import subprocess

    target = tmp_path / "dropped.txt"
    code = ("import resource; from app.ingest.pdf_worker import _limit; _limit(resource.RLIMIT_FSIZE, 0)\n"
            f"f = open({str(target)!r}, 'w')\n"
            "try:\n    f.write('x' * 10); f.flush(); print('wrote')\n"
            "except OSError as e:\n    print('blocked', e.errno)")  # fmt: skip
    # The interpreter and the code are both ours; nothing here comes from input.
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=20)  # noqa: S603
    assert out.stdout.strip() == "blocked 27"
    assert target.stat().st_size == 0


async def test_output_must_match_the_declared_page_count(tmp_path: Path) -> None:
    pdf = write(tmp_path, make_pdf([["x"]]))
    liar = 'print(\'{"pages": 3}\'); print(\'{"page": 1, "label": null, "text": "x"}\')'
    with pytest.raises(PdfRejected, match="1 of 3 pages"):
        await extract_pages(pdf, FAST, worker=[sys.executable, "-c", liar])


async def test_generated_handbook_page_chunks_cleanly(tmp_path: Path) -> None:
    from app.ingest.pdf_docs import page_chunks

    pdf = write(tmp_path, make_pdf([handbook_page("hydraulic", "9-12"), [], handbook_page("brake", "9-13")]))
    chunks, empty = page_chunks("hb", await extract_pages(pdf, FAST))
    assert empty == 1
    assert {(c.page, c.page_label) for c in chunks} == {(1, "9-12"), (3, "9-13")}
    assert all(c.chunk_id.startswith(f"hb:p{c.page}:c") for c in chunks)


@pytest.mark.skipif(not SPIKE_AC.exists(), reason="spike corpus not downloaded")
async def test_real_faa_pdf_through_the_sandbox() -> None:
    pages = await extract_pages(SPIKE_AC)
    assert len(pages) == 646
    assert pages[400].label == "8-22"  # "Page 8-22 Par 8-37" footer
    assert "" not in "".join(p.text for p in pages)  # private-use glyphs cleaned
