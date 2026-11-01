"""How much of the SDR data does app/ingest/sdr_models.py normalise? Reads spikes/data/sdr/SDR-*.csv.

Run: uv run python scripts/measure_sdr_models.py
"""

import collections
import csv
import sys
from pathlib import Path

from app.ingest.sdr_models import normalise_make, normalise_model

csv.field_size_limit(10**9)
files = sorted((Path(__file__).parent.parent / "spikes" / "data" / "sdr").glob("SDR-*.csv"))
if not files:
    sys.exit("no SDR files: run spikes/fetch_data.py first")
n, with_family, by_family, unmapped = 0, 0, collections.Counter(), collections.Counter()
samples: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
for f in files:
    for d in csv.DictReader(f.open(newline="", encoding="utf-8")):
        n += 1
        model, family = normalise_model(d["AircraftMake"], d["AircraftModel"])
        if family:
            with_family += 1
            by_family[family] += 1
            samples[model][d["AircraftModel"].strip()] += 1
        else:
            unmapped[f"{normalise_make(d['AircraftMake'])} {d['AircraftModel'].strip()}"] += 1
print(f"{n:,} reports; {with_family:,} ({with_family / n:.1%}) mapped to a model family")
print("families:", by_family.most_common(20))
print("largest unmapped:", unmapped.most_common(15))
print("canonical model <- raw designations (largest 12):")
for model in sorted(samples, key=lambda m: -sum(samples[m].values()))[:12]:
    print(f"   {model:14s} <- {samples[model].most_common(5)}")
