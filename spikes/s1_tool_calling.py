"""Spike 1: do candidate OpenRouter models call our tools reliably, and return valid structured output?

Run: uv run python s1_tool_calling.py [--repeats 2] [model ...]
Every request's real cost (usage.cost) is written to .cache/spend.jsonl; the run stops at SPIKE_BUDGET_USD.
"""

import argparse
import json
import statistics
import sys
import time
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from common import PROVIDER_PREFS, BudgetExceeded, check_budget, client, optional, record, strict_schema

DEFAULT_MODELS = ["anthropic/claude-haiku-5.5", "openai/gpt-6-luna", "deepseek/deepseek-v4.1-flash"]

class SearchDocumentsArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    sources: list[Literal["handbook", "ac", "ad"]] | None = None
    aircraft_make: str | None = None
    aircraft_model: str | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class QuerySdrArgs(BaseModel):
    aircraft_make: str | None = None
    aircraft_model: str | None = None
    jasc_code: str | None = Field(default=None, pattern=r"^\d{4}$")
    part_number: str | None = None
    text: str | None = None
    since: date | None = None
    until: date | None = None
    mode: Literal["list", "count_by_jasc", "count_by_part", "count_by_month"] = "list"
    limit: int = Field(default=20, ge=1, le=100)


class GetAircraftArgs(BaseModel):
    registration: str = Field(pattern=r"^MX-\d{3}$")


TOOLS = {
    "search_documents": (SearchDocumentsArgs, "Hybrid search over FAA AMT handbooks ('handbook'), AC 43.13-1B ('ac') and Airworthiness Directives ('ad'). Use aircraft_make/model to filter ADs."),
    "query_sdr": (QuerySdrArgs, "Query FAA Service Difficulty Reports (fleet-wide fault history) with structured filters. JASC codes are 4-digit ATA codes, e.g. 3230 landing gear retraction. Use a count_* mode for 'how often / most common' questions."),
    "get_aircraft": (GetAircraftArgs, "Get one aircraft from OUR fleet (registrations MX-001..MX-030): type, engines, hours, cycles, last 5 work orders."),
}
TOOL_SPECS = [
    {"type": "function", "function": {"name": n, "description": d, "parameters": strict_schema(m)}}
    for n, (m, d) in TOOLS.items()
]

SYSTEM = (
    "You are a maintenance copilot for an airline maintenance organisation. Use the tools to gather evidence. "
    "Today is 2026-10-28. Our fleet registrations look like MX-001. "
    "Call tools rather than answering from memory."
)

# (prompt, expected tool or None for 'should not call a tool', extra check on the parsed args)
CASES = [
    ("What does AC 43.13 say about inspecting control cables for broken wires?", "search_documents",
     lambda a: "cable" in a.query.lower() and a.sources in (None, ["ac"])),
    ("Are there any recent airworthiness directives on Boeing 737-800 main landing gear?", "search_documents",
     lambda a: a.sources in (None, ["ad"]) and "boeing" in (a.aircraft_make or a.query).lower()),
    ("What's the hours and cycles on MX-014, and its last work orders?", "get_aircraft",
     lambda a: a.registration == "MX-014"),
    ("Which JASC codes come up most often in SDRs for the CRJ-900 since January 2026?", "query_sdr",
     lambda a: a.mode == "count_by_jasc" and a.since is not None and a.since.year == 2026 and a.since.month == 1),
    ("List SDRs on Boeing 737-800 with JASC 3230 in 2025, max 10.", "query_sdr",
     lambda a: a.jasc_code == "3230" and a.limit == 10 and a.since is not None and a.since.year == 2025),
    ("Search only the powerplant handbook material for causes of high EGT on a turbine engine, top 3.", "search_documents",
     lambda a: a.sources == ["handbook"] and a.top_k == 3),
    ("Thanks, that's all for now.", None, None),
    ("Aircraft N12345 has a hydraulic leak near the left main gear, what should I check?", "search_or_sdr", None),  # N12345 is not in our fleet: must not be forced into get_aircraft
]


class Citation(BaseModel):
    chunk_id: str | None = None
    sdr_id: str | None = None
    quote: str = Field(max_length=300)


class Diagnosis(BaseModel):
    likely_causes: list[str] = Field(min_length=1, max_length=5)
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    citations: list[Citation] = Field(min_length=1)
    evidence_incomplete: bool = False
    safety_notes: list[str] = []


# SDR rows are real (SDR-2025.csv); the handbook chunk text is paraphrased for the spike.
DIAG_EVIDENCE = """MX-014 (B737-800): landing gear failed to retract after takeoff; retracted normally after QRH procedure.
Evidence:
[faa-amt-airframe:p612:c1] Landing gear retraction faults are commonly traced to the gear selector, proximity sensors and squat (air/ground) switches, or hydraulic pressure supply; perform a gear swing to isolate.
[sdr:SCNA20250106001] B737-800 JASC 3230 2025-01-06 "LANDING GEAR FAILED TO RETRACT AFTER TAKEOFF. ACCOMPLISHED QRH PROCEDURE AND LANDING GEAR RETRACTED NORMALLY AFTERWARDS."
[sdr:SCNA20250106002] B737-800 JASC 3230 2025-01-06 same event, supplemental: "PERFORMED LANDING GEAR SWING, NO FAULTS NOTED"
Produce a diagnosis for MX-014 citing the evidence by chunk_id / sdr_id."""
DIAG_IDS = {"faa-amt-airframe:p612:c1", "sdr:SCNA20250106001", "sdr:SCNA20250106002", "SCNA20250106001", "SCNA20250106002"}


def run_tool_case(c, model, prompt, expected, extra):
    check_budget()
    t = time.perf_counter()
    r = c.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        tools=TOOL_SPECS, tool_choice="auto", max_tokens=600,
        **optional(model, parallel_tool_calls=False, temperature=0),
        extra_body={"provider": PROVIDER_PREFS},
    )
    dt = time.perf_counter() - t
    record("tool", model, r.usage, dt)
    calls = r.choices[0].message.tool_calls or []
    if expected is None:
        return (not calls), dt, f"called={[x.function.name for x in calls]}"
    if not calls:
        return False, dt, "no tool call"
    name, raw = calls[0].function.name, calls[0].function.arguments
    if expected == "search_or_sdr":
        # N12345 is not ours: searching docs/SDRs is right; get_aircraft would fail validation -> FAIL.
        if name not in ("search_documents", "query_sdr"):
            return False, dt, f"called {name} {raw}"
        try:
            TOOLS[name][0].model_validate_json(raw)
            return True, dt, f"{name} {raw}"
        except ValidationError:
            return False, dt, f"{name} invalid args {raw}"
    if name != expected:
        return False, dt, f"wrong tool {name}"
    try:
        args = TOOLS[name][0].model_validate_json(raw)
    except ValidationError as e:
        return False, dt, f"invalid args {raw} ({e.error_count()} errors)"
    ok = extra(args) if extra else True
    return ok, dt, raw


def run_structured(c, model):
    check_budget()
    schema = strict_schema(Diagnosis)
    t = time.perf_counter()
    r = c.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": DIAG_EVIDENCE}],
        response_format={"type": "json_schema", "json_schema": {"name": "Diagnosis", "strict": True, "schema": schema}},
        # Reasoning tokens count against max_tokens: DeepSeek truncated its JSON at 1500.
        max_tokens=4000, **optional(model, temperature=0),
        extra_body={"provider": PROVIDER_PREFS, **optional(model, reasoning={"effort": "low"})},
    )
    dt = time.perf_counter() - t
    record("structured", model, r.usage, dt)
    try:
        d = Diagnosis.model_validate_json(r.choices[0].message.content or "")
    except ValidationError as e:
        return False, dt, f"invalid ({e.error_count()} errors)"
    ids = {x.chunk_id for x in d.citations} | {x.sdr_id for x in d.citations}
    cited_real = ids & DIAG_IDS
    return bool(cited_real), dt, f"causes={d.likely_causes[:2]} cited={sorted(i for i in ids if i)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*", default=DEFAULT_MODELS)
    ap.add_argument("--repeats", type=int, default=2)
    args = ap.parse_args()
    c = client()
    summary = {}
    try:
        for model in args.models:
            oks, lats, cost0 = [], [], None
            print(f"\n=== {model}")
            for rep in range(args.repeats):
                for prompt, exp, extra in CASES:
                    try:
                        ok, dt, detail = run_tool_case(c, model, prompt, exp, extra)
                    except BudgetExceeded:
                        raise
                    except Exception as e:  # provider errors count as failures
                        ok, dt, detail = False, 0.0, f"ERROR {type(e).__name__}: {e}"[:200]
                    oks.append(ok); lats.append(dt)
                    print(f"  [{'PASS' if ok else 'FAIL'}] r{rep} {prompt[:50]:50s} {dt:5.2f}s {detail[:110]}")
                try:
                    ok, dt, detail = run_structured(c, model)
                except BudgetExceeded:
                    raise
                except Exception as e:
                    ok, dt, detail = False, 0.0, f"ERROR {type(e).__name__}: {e}"[:200]
                print(f"  [{'PASS' if ok else 'FAIL'}] r{rep} structured Diagnosis {dt:5.2f}s {detail[:110]}")
                summary.setdefault(model, {}).setdefault("structured", []).append(ok)
            summary[model]["tool_pass"] = f"{sum(oks)}/{len(oks)}"
            summary[model]["p50_s"] = round(statistics.median(lats), 2)
            summary[model]["max_s"] = round(max(lats), 2)
    except BudgetExceeded as e:
        print(f"\nSTOPPED: {e}")
    print("\nSUMMARY")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    sys.exit(main())
