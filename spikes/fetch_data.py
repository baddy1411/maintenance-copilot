"""Download the spike corpus listed in docs/DATA.md into spikes/data/ (git-ignored).

All sources are official US government endpoints that allow scripted access:
- FAA maintenance handbooks + AC 43.13-1B (PDF)
- FAA Airworthiness Directives: metadata from the Federal Register API, text from govinfo.gov (updated daily)
- FAA Service Difficulty Reports, yearly CSV (updated continuously)

Run: uv run python fetch_data.py [--ad-years 2020-2026] [--sdr-years 2024-2026]
Re-running only fetches what is missing; ADs are incremental by document number.
"""

import argparse
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from common import ROOT

UA = {"User-Agent": "maintenance-copilot-spike/0.1 (research; contact via GitHub repo)"}
DATA = ROOT / "data"
HANDBOOKS = {
    "faa-amt-general": "https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/amtg_handbook.pdf",
    "faa-amt-airframe": "https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/FAA-H-8083-31B_Aviation_Maintenance_Technician_Handbook.pdf",
    "faa-amt-powerplant": "https://www.faa.gov/regulations_policies/handbooks_manuals/aviation/amt_powerplant_handbook.pdf",
    "faa-ac-43-13-1b": "https://www.faa.gov/documentLibrary/media/Advisory_Circular/AC_43.13-1B_w-chg1.pdf",
}
SDR_URL = "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv"
FR_API = "https://www.federalregister.gov/api/v1/documents.json"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
        return r.read()


def years(spec: str) -> range:
    a, _, b = spec.partition("-")
    return range(int(a), int(b or a) + 1)


def fetch_handbooks() -> None:
    (DATA / "faa").mkdir(parents=True, exist_ok=True)
    for doc_id, url in HANDBOOKS.items():
        path = DATA / "faa" / f"{doc_id}.pdf"
        if not path.exists():
            path.write_bytes(get(url))
        print(f"{doc_id:22s} {path.stat().st_size:>11,} bytes  sha256={hashlib.sha256(path.read_bytes()).hexdigest()[:16]}…")


def fetch_sdr(yrs: range) -> None:
    (DATA / "sdr").mkdir(parents=True, exist_ok=True)
    for y in yrs:
        path = DATA / "sdr" / f"SDR-{y}.csv"
        if not path.exists():
            path.write_bytes(get(SDR_URL.format(year=y)))
        print(f"SDR-{y}.csv {path.stat().st_size:>11,} bytes")


def fetch_ads(yrs: range) -> None:
    out = DATA / "ads"
    out.mkdir(parents=True, exist_ok=True)
    # Metadata comes from the Federal Register API; full text from GPO's govinfo.gov (same official text).
    # federalregister.gov's own full_text URLs return a bot-check page to scripts, so we don't use them.
    fields = ["document_number", "title", "publication_date", "effective_on", "pdf_url", "html_url", "cfr_references"]
    new = 0
    for y in yrs:
        page = 1
        while True:
            q = [("per_page", "100"), ("page", str(page)), ("order", "oldest"),
                 ("conditions[agencies][]", "federal-aviation-administration"),
                 ("conditions[type][]", "RULE"),
                 ("conditions[term]", '"airworthiness directives"'),
                 ("conditions[publication_date][year]", str(y))] + [("fields[]", f) for f in fields]
            res = json.loads(get(f"{FR_API}?{urllib.parse.urlencode(q)}"))
            for doc in res.get("results", []):
                path = out / f"{doc['document_number']}.json"
                if path.exists() or not doc["title"].startswith("Airworthiness Directives"):
                    continue
                text_url = doc["pdf_url"].replace("/pdf/", "/html/").removesuffix(".pdf") + ".htm"
                html = get(text_url).decode("utf-8", errors="replace")
                body = re.search(r"<pre>(.*)</pre>", html, re.S)
                text = re.sub(r"<[^>]+>", "", body.group(1)) if body else ""
                # Never store a block page or error page as a document.
                if "Airworthiness Directive" not in text or "Request Access" in html:
                    print(f"  SKIP {doc['document_number']}: unexpected content from {text_url}")
                    continue
                doc["text_url"], doc["text"] = text_url, text
                path.write_text(json.dumps(doc))
                new += 1
                time.sleep(0.1)  # be polite to the API
            if page >= res.get("total_pages", 1):
                break
            page += 1
        print(f"ADs {y}: {sum(1 for p in out.glob('*.json') if json.loads(p.read_text())['publication_date'].startswith(str(y)))} on disk")
    print(f"ADs fetched this run: {new}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ad-years", default="2020-2026")
    ap.add_argument("--sdr-years", default="2024-2026")
    a = ap.parse_args()
    fetch_handbooks()
    fetch_sdr(years(a.sdr_years))
    fetch_ads(years(a.ad_years))
