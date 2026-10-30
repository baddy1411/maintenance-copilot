"""Cited answers (DESIGN §6.1, §7.2): prompt building, the streaming splitter, citation checks, the verdict.

Pure functions, no I/O. The service (app/qa/service.py) wires them to retrieval and the gateway.

The model writes the answer with [n] markers, then a ---SOURCES--- line, then one `[n] "quote"` per
citation. While streaming:
  - the answer text goes to the client as it arrives;
  - everything after the delimiter is held back and parsed at the end;
  - the start is held back until it can't be the NOT_FOUND sentinel, so the user never sees it.
At the end each quote must be a normalised substring of passage n. If it isn't, the citation is dropped.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.retrieval.search import Hit

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "answer.md"
SYSTEM_PROMPT = PROMPT_PATH.read_text()
PROMPT_VERSION = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]

DELIMITER = "---SOURCES---"
SENTINEL = "NOT_FOUND"
MAX_UNSUPPORTED = 0.30  # DESIGN §6.1: more than 30 % unsupported sentences and the answer is refused
MIN_QUOTE_WORDS = 3
REFUSAL = ("I couldn't find this in the FAA sources I have: the AMT handbooks, AC 43.13-1B and "
           "Airworthiness Directives since 2020. For type-specific procedures, check the manufacturer's "
           "maintenance manual.")  # fmt: skip

Verdict = Literal["answered", "not_found", "refused_unsupported", "generation_unavailable"]


# ---- prompt -------------------------------------------------------------------------------------------


def _attr(value: object) -> str:
    return str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def passage_block(n: int, h: Hit) -> str:
    """Provenance on every passage, so the model can cite and flag supersession. The text is escaped,
    so a passage can't close its own tag and inject instructions outside it (DESIGN §7.2)."""
    attrs = {"n": n, "source": h.source, "doc": h.doc_id, "title": h.title}
    if h.page_label or h.page:
        attrs["page"] = h.page_label or f"PDF p. {h.page}"
    if h.ad_number:
        attrs["ad"] = h.ad_number
    if h.superseded_by:
        attrs["superseded_by"] = h.superseded_by
    head = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items())
    body = h.text.replace("<", "&lt;").replace(">", "&gt;")
    return f"<passage {head}>\n{body}\n</passage>"


def build_messages(question: str, hits: list[Hit]) -> list[dict[str, str]]:
    passages = "\n\n".join(passage_block(i, h) for i, h in enumerate(hits, start=1))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"<passages>\n{passages}\n</passages>\n\nQuestion: {question}"},
    ]


# ---- streaming ----------------------------------------------------------------------------------------


class SourcesSplitter:
    """Feed model deltas in; get back the text that's safe to show now.

    Two things are held back: anything that could still turn out to be the start of the delimiter (it can
    arrive split across deltas, e.g. "---SOU" + "RCES---"), and the opening characters until they can't be
    the NOT_FOUND sentinel."""

    def __init__(self) -> None:
        self.answer = ""  # everything before the delimiter
        self.sources = ""  # everything after it
        self._pending = ""
        self._in_sources = False
        self._started = False  # becomes True once the opening can't be the sentinel

    def feed(self, delta: str) -> str:
        if self._in_sources:
            self.sources += delta
            return ""
        self._pending += delta
        if DELIMITER in self._pending:
            before, after = self._pending.split(DELIMITER, 1)
            self._in_sources, self.sources, self._pending = True, after, ""
            return self._release(before, final=True)
        # Keep back the longest tail that is a prefix of the delimiter.
        keep = max((k for k in range(1, len(DELIMITER)) if self._pending.endswith(DELIMITER[:k])), default=0)
        ready, self._pending = (self._pending[:-keep], self._pending[-keep:]) if keep else (self._pending, "")
        return self._release(ready, final=False)

    def _release(self, text: str, final: bool) -> str:
        self.answer += text
        if not self._started:
            opening = self.answer.lstrip()
            if (not final and len(opening) < len(SENTINEL) and SENTINEL.startswith(opening)) or self.is_not_found():
                return ""
            self._started = True
            return self.answer  # everything held so far
        return text

    def finish(self) -> str:
        """End of stream: release whatever is still held."""
        tail = "" if self._in_sources else self._pending
        self._pending = ""
        return self._release(tail, final=True) if tail or not self._started else ""

    def is_not_found(self) -> bool:
        return self.answer.strip().startswith(SENTINEL)


# ---- citations and verdict ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    n: int
    chunk_id: str
    doc_id: str
    title: str
    source: str
    page: int | None
    page_label: str | None
    ad_number: str | None
    superseded_by: str | None
    quote: str


SOURCE_LINE = re.compile(r'^\s*\[(\d+)\]\s*["\u201c](.+?)["\u201d]\s*$', re.MULTILINE)
MARKER = re.compile(r"\[(\d+)\]")


# Curly quotes and en/em dashes -> ASCII, so a quote copied with different typography still matches.
TYPOGRAPHIC = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-"})


def normalise(text: str) -> str:
    """Case, whitespace, quote and dash differences don't count against a quote."""
    t = text.lower().translate(TYPOGRAPHIC)
    t = re.sub(r"[^\w\s'\-\.,;:/%()]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def validate_citations(sources_text: str, hits: list[Hit]) -> tuple[list[Citation], list[str]]:
    """Valid citations, and the reasons the others were dropped."""
    valid: list[Citation] = []
    dropped: list[str] = []
    seen: set[int] = set()
    for n_str, quote in SOURCE_LINE.findall(sources_text):
        n = int(n_str)
        if n in seen:
            continue
        if not 1 <= n <= len(hits):
            dropped.append(f"[{n}] no such passage")
            continue
        h = hits[n - 1]
        if len(quote.split()) < MIN_QUOTE_WORDS:
            dropped.append(f"[{n}] quote too short")
            continue
        if normalise(quote) not in normalise(h.text):
            dropped.append(f"[{n}] quote not found in passage")
            continue
        seen.add(n)
        valid.append(Citation(n, h.chunk_id, h.doc_id, h.title, h.source, h.page, h.page_label, h.ad_number,
                              h.superseded_by, quote.strip()))  # fmt: skip
    return valid, dropped


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"(])|\n+", text.strip())
    return [p.strip() for p in parts if len(p.split()) >= 4]  # fragments and headings don't count


def support(answer: str, citations: list[Citation]) -> tuple[int, int]:
    """(unsupported sentences, sentences). A sentence is supported if it has a marker for a valid citation."""
    ok = {c.n for c in citations}
    sents = sentences(answer)
    unsupported = sum(1 for s in sents if not {int(m) for m in MARKER.findall(s)} & ok)
    return unsupported, len(sents)


def verdict(answer: str, citations: list[Citation]) -> tuple[Verdict, int, int]:
    if answer.strip().startswith(SENTINEL):
        return "not_found", 0, 0
    unsupported, total = support(answer, citations)
    if total == 0 or not citations or unsupported / total > MAX_UNSUPPORTED:
        return "refused_unsupported", unsupported, total
    return "answered", unsupported, total
