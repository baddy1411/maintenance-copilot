"""Word-window chunking (DESIGN §8): 220 words, 40-word overlap, hard cap 450 tokens per chunk."""

import math
from collections.abc import Iterator
from dataclasses import dataclass

WORDS, OVERLAP, MAX_TOKENS = 220, 40, 450
# Measured on the spike corpus: 71.4M chars / 17.98M voyage tokens = 3.97 chars per token. 3.9 leaves a
# little margin. This is NOT the spend estimate (app.llm.catalog), which is pessimistic on purpose.
CHARS_PER_TOKEN = 3.9
MIN_CHARS = 80  # drop trailing slivers


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    ord: int
    text: str
    page: int | None = None
    page_label: str | None = None


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def _windows(words: list[str]) -> Iterator[str]:
    for start in range(0, max(len(words), 1), WORDS - OVERLAP):
        yield " ".join(words[start : start + WORDS])
        if start + WORDS >= len(words):
            return


def _capped(text: str, cap: int) -> Iterator[str]:
    """Split a window in half until it fits the token cap (tables of part numbers are token-dense)."""
    if estimate_tokens(len(text)) <= cap:
        yield text
        return
    words = text.split()
    if len(words) < 2:
        yield text[: cap * 3]
        return
    mid = len(words) // 2
    yield from _capped(" ".join(words[:mid]), cap)
    yield from _capped(" ".join(words[mid:]), cap)


def chunk_text(
    doc_id: str, text: str, prefix: str = "", page: int | None = None, start_ord: int = 0,
    page_label: str | None = None,
) -> list[Chunk]:  # fmt: skip
    """`prefix` (for ADs: number, make, models) is added to every chunk so the identifiers survive
    chunking (SPIKE §2). It counts towards the token cap."""
    out: list[Chunk] = []
    cap = MAX_TOKENS - estimate_tokens(len(prefix))
    for window in _windows(text.split()):
        for body in _capped(window, cap):
            piece = f"{prefix}{body}"
            if len(body) < MIN_CHARS:
                continue
            ord_ = start_ord + len(out)
            suffix = f":p{page}:c{ord_}" if page is not None else f":c{ord_}"
            out.append(Chunk(chunk_id=f"{doc_id}{suffix}", doc_id=doc_id, ord=ord_, text=piece, page=page,
                             page_label=page_label))  # fmt: skip
    return out
