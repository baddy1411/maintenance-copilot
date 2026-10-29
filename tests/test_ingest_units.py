"""AD parsing and chunking, on real ADs (tests/fixtures/ads, US government works)."""

import json
from pathlib import Path

import pytest

from app.ingest.ad_parse import expand_models, make_from_title, normalise_ad, parse_ad, text_from_govinfo_html
from app.ingest.ads import ad_prefix, govinfo_text_url, invalid_reason
from app.ingest.chunk import MAX_TOKENS, chunk_text, estimate_tokens

FIXTURES = Path(__file__).parent / "fixtures" / "ads"


def load(name: str) -> dict[str, str]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(
    ("fixture", "ad_number", "kind", "supersedes", "some_models"),
    [
        ("supersedes", "2020-18-08", "original", ["2019-12-18"], ["R44"]),
        ("model_range", "2020-26-09", "original", [], ["737-100", "737-200", "737-200C", "737-500"]),
        ("header_typo", "2021-15-09", "original", [], ["AW139"]),  # "Amendment 39-21656 AD ..." (no ';')
        ("correction", "2021-17-14", "correction", [], ["G280"]),
        ("revision", "2020-25-03R1", "revision", ["2020-25-03"], ["A318-111"]),
    ],
)
def test_parse_real_ads(fixture: str, ad_number: str, kind: str, supersedes: list[str], some_models: list[str]) -> None:
    d = load(fixture)
    p = parse_ad(d["text"], d["title"], d["document_number"])
    assert p is not None
    assert (p.ad_number, p.kind, p.supersedes) == (ad_number, kind, supersedes)
    assert set(some_models) <= set(p.models)
    # Real wording varies: "This AD applies to", "(1) This AD applies to", "This action applies to".
    assert p.applicability and "applies to" in p.applicability[:40]


def test_no_header_means_not_an_ad() -> None:
    assert (
        parse_ad("Airworthiness Directive text without the docket header", "Airworthiness Directives; X", "1") is None
    )


@pytest.mark.parametrize(
    ("raw", "norm"),
    [("2020-25-03", "2020-25-03"), ("2020-25-03R1", "2020-25-03R1"), ("92-02-14 R1", "1992-02-14R1"),
     ("16-15-01", "2016-15-01")],
)  # fmt: skip
def test_normalise_ad(raw: str, norm: str) -> None:
    assert normalise_ad(raw) == norm


def test_expand_models_fills_dash_suffixes() -> None:
    assert expand_models(["737-100", "-200", "-200C", "A320-214", "-232"]) == [
        "737-100", "737-200", "737-200C", "A320-214", "A320-232"]  # fmt: skip


@pytest.mark.parametrize(
    ("title", "make"),
    [
        ("Airworthiness Directives; The Boeing Company Airplanes", "The Boeing Company"),
        ("Airworthiness Directives; Bombardier, Inc., Airplanes", "Bombardier, Inc."),
        ("Airworthiness Directives; Engine Alliance Turbofan Engines", "Engine Alliance"),
        ("Airworthiness Directives; Gulfstream Aerospace Corporation Airplanes; Correction",
         "Gulfstream Aerospace Corporation"),
    ],
)  # fmt: skip
def test_make_from_title(title: str, make: str) -> None:
    assert make_from_title(title) == make


def test_govinfo_text_is_extracted_and_unescaped() -> None:
    page = "<html><body><pre>Smith &amp; Wesson <b>AD</b> 2026-01-01</pre></body></html>"
    assert text_from_govinfo_html(page) == "Smith & Wesson AD 2026-01-01"


def test_govinfo_url_from_pdf_url() -> None:
    assert (govinfo_text_url("https://www.govinfo.gov/content/pkg/FR-2026-10-01/pdf/2026-20098.pdf")
            == "https://www.govinfo.gov/content/pkg/FR-2026-10-01/html/2026-20098.htm")  # fmt: skip


def test_block_pages_and_junk_are_invalid() -> None:
    good = load("supersedes")["text"]
    assert invalid_reason(f"<pre>{good}</pre>", good) is None
    assert invalid_reason("<h1>Request Access</h1>", "Request Access") == "block page"
    assert invalid_reason("<pre>x</pre>", "x" * 5000) == "no 'Airworthiness Directive' in text"
    assert "length" in (invalid_reason("", "Airworthiness Directive") or "")


def test_ad_chunks_carry_the_prefix_and_respect_the_cap() -> None:
    d = load("model_range")
    p = parse_ad(d["text"], d["title"], d["document_number"])
    assert p is not None
    prefix = ad_prefix(p)
    chunks = chunk_text("ad:x", d["text"], prefix=prefix)
    assert len(chunks) > 3
    assert all(c.text.startswith("[AD 2020-26-09: The Boeing Company 737-100") for c in chunks)
    assert all(estimate_tokens(len(c.text)) <= MAX_TOKENS for c in chunks)
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chunk_id == "ad:x:c0"
    # 40-word overlap between consecutive windows
    body0, body1 = chunks[0].text.removeprefix(prefix).split(), chunks[1].text.removeprefix(prefix).split()
    assert body0[-40:] == body1[:40]


def test_dense_text_is_split_under_the_cap() -> None:
    table = " ".join(f"P/N-{i:06d}-ABCDEFGHIJ" for i in range(220))  # 220 long tokens, one window
    chunks = chunk_text("doc", table)
    assert len(chunks) > 1
    assert all(estimate_tokens(len(c.text)) <= MAX_TOKENS for c in chunks)


def test_control_characters_are_stripped() -> None:
    # 276 real ADs carry NULs before their separator lines; Postgres text can't hold NUL.
    page = "<pre>[FR Doc No: 2020-07399]\n\n\x00=====\x07\nAirworthiness Directive\ttext</pre>"
    assert text_from_govinfo_html(page) == "[FR Doc No: 2020-07399]\n\n=====\nAirworthiness Directive\ttext"


def test_indented_section_headers_and_numeric_models() -> None:
    # 2026 FR text indents headers ("\n\n (d) Subject"); MD 369D/500N designators have no dash.
    text = ("[Docket No. FAA-2026-1; Amendment 39-1; AD 2026-21-05]\n\n (b) Affected ADs\n\n    None.\n\n"
            " (c) Applicability\n\n    This AD applies to MD Helicopters, LLC (MDHI) Model 369D, 369E, and 500N "
            "helicopters, certificated in any category.\n\n (d) Subject\n\n    JASC Code 6310.")  # fmt: skip
    p = parse_ad(text, "Airworthiness Directives; MD Helicopters, LLC Helicopters", "2026-21664")
    assert p is not None and p.applicability and p.applicability.startswith("This AD applies to MD")
    assert p.models == ["369D", "369E", "500N"]
