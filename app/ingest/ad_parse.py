"""Metadata from Airworthiness Directive text (Federal Register final rules, as published on govinfo).

Measured on the 2,545 ADs from 2020-2026 in the spike corpus. Coverage figures are in the issue #5 notes
and are re-checked by `scripts/measure_ad_parse.py`. Every field is best effort except the AD number: a
document without one isn't treated as an AD and is quarantined.
"""

import html
import re
from dataclasses import dataclass, field
from typing import Literal

DocKind = Literal["original", "correction", "revision"]

# Bump on any change to parsing or chunking output. It's part of the stored content hash, so documents
# already ingested are re-processed (and re-embedded) on the next sync instead of being skipped as unchanged.
PARSER_VERSION = "3"

# The header block ends with the AD number, written several ways:
#   "Amendment 39-22086; AD 2020-25-03R1]"   "AD-2025-05-15]"   "AD No. 2020-19-12]"
#   "Amendment 39-23319; 2026-08-11]"        "AD 92-02-14 R1]" (two-digit year)
#   "Amendment 39-21656 AD 2021-15-09]"      "Amendment 39-21749-; AD 2021-20-11]" (typos in the FR text)
HEADER_AD = re.compile(
    r"Amendment\s+39-\d+-?;?\s*(?:AD(?:\s+No\.)?[\s-]*)?(\d{2,4}-\d{2}-\d{2})\s*(R\d+)?\s*\]", re.IGNORECASE
)
AD_REF = r"(\d{2,4}-\d{2}-\d{2}(?:\s?R\d+)?)"
REPLACES = re.compile(rf"\b(?:replaces|supersedes|revises)\s+AD\s+{AD_REF}", re.IGNORECASE)
# Section headers may be indented ("\n\n (d) Subject" in 2026 FR text).
SECTION = r"\({letter}\)\s+{name}\s+(.*?)\n\s*\n\s*\(\w\)"
JASC = re.compile(r"\(JASC\)\s+Codes?:?\s*(\d{4})((?:\s*(?:,|and)\s*\d{4})*)")
# Model designators after "Model"/"Models", e.g. "787-8", "AS355F1", "CL-600-2B19 (Regional Jet Series 705)".
MODEL_LIST = re.compile(
    r"\bModels?\s+(.+?)\s+(?:airplanes|helicopters|engines|gliders|sailplanes|"
    r"propellers|turbofan|turboprop|turboshaft|rotorcraft|balloons)",
    re.IGNORECASE | re.S,
)
DESIGNATOR = re.compile(
    r"(?<![\w-])-[A-Za-z0-9]+\b|\b[A-Z0-9][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)+\b|\b[A-Z]{1,4}\d[A-Za-z0-9]*\b"
    # purely numeric designators inside the "Model ... helicopters" span: 369D, 500N, 172S, 206B
    r"|\b\d{2,4}[A-Z]{0,3}\b"
)
TITLE_KIND = re.compile(
    r"[\s,]+(?:(?:Turbofan|Turboprop|Turboshaft|Turbojet|Reciprocating)\s+)?"
    r"(?:Airplanes|Helicopters|Engines|Gliders|Sailplanes|Propellers|Rotorcraft|Balloons)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedAD:
    ad_number: str  # normalised: "2020-25-03R1", "1992-02-14R1" (two-digit years expanded)
    kind: DocKind
    make: str
    supersedes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    jasc: list[str] = field(default_factory=list)
    applicability: str | None = None


# GPO formatting artifacts: 276 of 2,545 ADs (2020-2026) carry NULs and other control characters
# before their separator lines. Postgres text can't hold NUL, and none of them carry meaning.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_from_govinfo_html(page: str) -> str:
    """The FR text sits in one <pre> block. Tags stripped, entities decoded (&amp; -> &), control
    characters removed."""
    m = re.search(r"<pre>(.*)</pre>", page, re.S)
    return CONTROL.sub("", html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))) if m else ""


def normalise_ad(number: str, revision: str | None = None) -> str:
    number = re.sub(r"\s+", "", number)
    rev = ""
    if m := re.match(r"(.*?)(R\d+)$", number):
        number, rev = m.group(1), m.group(2)
    year, rest = number.split("-", 1)
    if len(year) == 2:
        year = ("19" if int(year) > 50 else "20") + year
    return f"{year}-{rest}{(revision or rev).upper()}"


def expand_models(tokens: list[str]) -> list[str]:
    """'737-100', '-200', '-200C' -> 737-100, 737-200, 737-200C. A bare suffix takes the previous full
    designator's base (everything before its last dash)."""
    out: list[str] = []
    base = None
    for t in tokens:
        if t.startswith("-"):
            if base:
                out.append(base + t)
            continue
        out.append(t)
        base = t.rsplit("-", 1)[0] if "-" in t else t
    return list(dict.fromkeys(out))


def _section(text: str, letter: str, name: str) -> str | None:
    m = re.search(SECTION.format(letter=letter, name=name), text, re.S)
    return " ".join(m.group(1).split()) if m else None


def make_from_title(title: str) -> str:
    """'Airworthiness Directives; The Boeing Company Airplanes; Correction' -> 'The Boeing Company'."""
    t = title.removeprefix("Airworthiness Directives;").strip()
    t = re.sub(r";\s*Correction$", "", t, flags=re.IGNORECASE)
    return TITLE_KIND.sub("", t).strip().rstrip(",;")


def parse_ad(text: str, title: str, document_number: str) -> ParsedAD | None:
    head = HEADER_AD.search(text)
    if not head:
        return None
    ad_number = normalise_ad(head.group(1), head.group(2))
    is_correction = document_number.startswith("C") or bool(re.search(r";\s*Correction$", title, re.IGNORECASE))
    kind: DocKind = "correction" if is_correction else ("revision" if re.search(r"R\d+$", ad_number) else "original")

    affected = _section(text, "b", "Affected ADs") or ""
    supersedes = sorted({normalise_ad(n) for n in REPLACES.findall(affected)} - {ad_number})
    applicability = _section(text, "c", "Applicability")
    models: list[str] = []
    if applicability and (m := MODEL_LIST.search(applicability)):
        # Line wraps in the FR text leave "- 200" for "-200".
        listed = re.sub(r"(?<=\s)-\s+(?=[A-Za-z0-9])", "-", m.group(1))
        models = expand_models(DESIGNATOR.findall(listed))
    jasc: list[str] = []
    if j := JASC.search(text):
        jasc = [j.group(1), *re.findall(r"\d{4}", j.group(2))]
    return ParsedAD(ad_number=ad_number, kind=kind, make=make_from_title(title), supersedes=supersedes,
                    models=models[:50], jasc=jasc, applicability=applicability)  # fmt: skip
