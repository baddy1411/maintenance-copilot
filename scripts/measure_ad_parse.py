"""How well does app/ingest/ad_parse.py cover real ADs? Reads the spike corpus (spikes/data/ads/*.json).

Run: uv run python scripts/measure_ad_parse.py
"""

import collections
import json
import sys
from pathlib import Path

from app.ingest.ad_parse import parse_ad

corpus = sorted((Path(__file__).parent.parent / "spikes" / "data" / "ads").glob("*.json"))
if not corpus:
    sys.exit("no spike corpus: run spikes/fetch_data.py first")
c: collections.Counter[str] = collections.Counter()
misses = []
for path in corpus:
    doc = json.loads(path.read_text())
    p = parse_ad(doc["text"], doc["title"], doc["document_number"])
    c["docs"] += 1
    if p is None:
        misses.append(doc["document_number"])
        continue
    c["ad_number"] += 1
    c[f"kind:{p.kind}"] += 1
    c["supersedes>0"] += bool(p.supersedes)
    c["models>0"] += bool(p.models)
    c["jasc>0"] += bool(p.jasc)
    c["applicability"] += bool(p.applicability)
    c["make"] += bool(p.make)
n = c["docs"]
for k, v in sorted(c.items()):
    print(f"{k:16s} {v:5d}  {v / n:6.1%}")
print("no AD number:", misses)
