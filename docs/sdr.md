# Service Difficulty Reports

How the FAA's Service Difficulty Reports (SDRs) get into the database, how aircraft names are normalised, and how the agent queries them with `query_sdr`. This is the detailed companion to DESIGN §5.1 (`sdr`, `model_aliases`), §6.2 (the weekly sync) and [ADR-0008](adr/0008-structured-sql-tools.md) (structured SQL tools).

**Code:**
- `app/ingest/sdr.py`: parser, checks, sync.
- `app/ingest/sdr_models.py`: make and model normalisation.
- `app/agent/tools.py`: `query_sdr`. `app/agent/schemas.py`: `QuerySdrArgs`.
- `app/migrations/versions/0009_sdr_columns.py`.

**Tests:** `tests/test_sdr_models.py` (48), `tests/test_ingest_sdr.py` (29), and `tests/test_tools_contract.py` (7, the whole tool layer).

**Measurement:** `scripts/measure_sdr_models.py`.

**Date:** 2026-11-01.

## 1. Using it

```bash
uv run copilot ingest-sdr                   # this year and the 2 before; no model calls, no spend
uv run copilot ingest-sdr --years 2025      # one year
```

The worker also runs `sync_sdr` every Sunday at 04:45 UTC, with a queueing lock and 3 attempts.

## 2. The source

| Fact | Value (measured on the 2024–2026 files) |
| --- | --- |
| URL | `https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv` (on the ingestion allow-list) |
| Size | about 34 MB for a full year |
| Rows | 2024: 66,080 · 2025: 67,652 · 2026: 49,860 on 2026-11-01 (49,178 on 2026-10-28) |
| Columns | **76**, with identical headers in every year. Earlier notes in DATA.md said 80; that was wrong and is now corrected |
| Encoding | UTF-8 with 0 wrong-width rows. About 130 rows contain control characters (NUL, BEL…), and about 430 discrepancies span several lines |
| Key | `OperatorControlNumber`, unique within each year and across years |
| Dates | `DifficultyDate` is always `MM/DD/YYYY`; `SubmissionDate` is ISO. Each file's year is the difficulty year |
| Empty key columns | make and model 0.1–0.2 %, JASC ≈ 0 %, registration about 0.6 % |
| Discrepancy length | median about 200 characters, longest 9,109 |
| Changes | the current year grows daily, and back years get late or amended reports. That is why each sync re-reads three years |

## 3. Checks: a file is applied whole or not at all

`parse_file` and `apply_year` refuse a file with `FileRejected`. The reason goes to `quarantine` (URL + body hash), the run is marked failed, and **reports already stored stay as they are**.

| Check | Rule | Why |
| --- | --- | --- |
| Encoding | must decode as UTF-8 | a different encoding means a different producer or a corrupted file |
| Header | must equal the pinned 76 column names, in order | a renamed or moved column would silently put data in the wrong field. A block page (HTML) fails here too |
| Unreadable rows | no id, unreadable date, wrong width or duplicate id: at most 1 %. These rows are dropped and counted | a few bad rows are normal; many means the format changed |
| Empty key columns | `AircraftMake`, `AircraftModel`, `JASCCode` and `Discrepancy` may each be empty in at most 2 % of rows (measured: ≤ 0.6 %) | a column that is suddenly empty means the export changed |
| **Truncation** | the file must hold at least 90 % (`sdr_min_kept`) of the live reports already stored for its year | a cut-off download would otherwise **tombstone thousands of real reports** |

## 4. Make and model normalisation

SDRs give the FAA registry's abbreviated make and the manufacturer's model designation, usually with a customer code. People ask about "737-800", "CRJ900" or "A320". So each report gets three normalised fields, and the raw make and model are kept:
- `make` (Boeing, Bombardier…);
- `model` (737-800, CRJ900…);
- `model_family` (737, CRJ, A320, E-Jet…).

| Raw | Normalised | Rule |
| --- | --- | --- |
| `BOEING 7378H4`, `737823` | 737-800 / 737 | 737 + series digit + customer code |
| `BOEING 7378` (4,789 reports), `7379` | **737-8 / 737-9 (MAX)** | No customer code. Checked against operators: Southwest, American and United file their MAX fleets this way. Treating them as 737-800s would have been the easy mistake |
| `BOEING 737924ER`, `76734AF`, `777FS2` | 737-900ER, 767-300F, 777F | ER and F suffixes kept |
| `AIRBUS A321253NX`, `A300B4622R` | A321-253NX / A321, A300-600R / A300 | |
| `AIRBUS BD5001A11` | A220-300 / A220 | the A220 keeps its Bombardier type designation |
| `CNDAIR CL6002D24`, `CL6002C10`, `CL6002B19` | CRJ900, CRJ700, CRJ200 / CRJ | CL-600 type-certificate variants |
| `EMB ERJ170200LR`, `EMB145XR`, `EMB505` | E175 / E-Jet, ERJ145 / ERJ, Phenom 300 | |
| `DOUG MD11F`, `MD88`, `DC983` | MD-11F, MD-88 / MD-80, DC-9 | `DC983` is a DC-9-83, not a "DC-98". A first version got this wrong; it was found by listing the DOUG models |

**Coverage:** 174,564 of 182,910 reports (**95.4 %**) get a family. The rest are mostly business jets and general aviation, such as Cessna 680A and Lockheed 382G. They keep their raw model and `model_family` is NULL, so they can still be found by exact model. Make codes were taken from the data: every code with more than about 10 reports is mapped, and others are title-cased.

**`model_aliases`:** every raw make, and every raw make + model, is recorded with its canonical form on each sync. The whole mapping can then be inspected in one table: locally, 73 makes and 753 make + model pairs.

**Query side:** the same rules normalise what the model or a person passes. Examples:
- "Boeing 737-800" (the fleet table's type) → 737-800;
- "Bombardier CRJ900 (CL-600-2D24)" → CRJ900;
- "737 MAX 8" → 737-8;
- "737" stays the family, not the 737-7;
- "A320neo" → A320.

## 5. Storage (migration 0009)

New columns on `sdr`:
- `model_family`, `operator_code`, `part_condition`, `part_location`, `nature` (the nature-of-condition codes), `submitted_on`;
- `source_year`, the file a report came from.

There is a new index on `(model_family, jasc, difficulty_date)`, plus indexes on `difficulty_date` and `source_year`. `tools_sdr` now exposes the new columns, except `source_year`. `ingest_runs.tombstoned` counts reports withdrawn per run, so #16 can alert on a spike.

**Not stored:** the registration (N-number) and the aircraft, engine and part serial numbers. No question needs them, and leaving them out keeps the tool from identifying individual aircraft (data minimisation). Engine, propeller and structural-location columns are also left out for now; adding them later is a parser version bump.

## 6. The sync, step by step

For each year, oldest first:
1. **Fetch** through `SafeFetcher`: allow-list, private-address block, redirects re-checked, retries. The size cap is raised to 150 MB for this source only, with a 300 s timeout.
2. **Parse and check** (§3). Text fields go through the same `clean()` as ADs and PDFs, which strips control characters and private-use glyphs.
3. **Hash** each row: SHA-256 over the parser version, the year and every stored field. A parser change therefore rewrites every report once, and nothing else does.
4. **Apply in one transaction:**
   1. take a per-year advisory lock, so a scheduled and a manual sync can't interleave;
   2. run the truncation check against the live count;
   3. `COPY` the rows into a temporary table (183k rows load in about 19 s end to end);
   4. **upsert** where the row hash differs or the report was tombstoned;
   5. **tombstone** live reports of that year that aren't in the file;
   6. upsert the aliases.

**A report whose difficulty date moves to another year** is tombstoned by its old year, then revived and moved by its new year. Years run oldest first, so it ends up live. This case is tested.

**Tombstones, not deletes:** the FAA withdraws or merges reports. Keeping a tombstone means a draft that cited a report can still show it, and a sudden spike in tombstones is visible.

**One failed year doesn't stop the others.** It is recorded in `errors` and the run is marked `failed`, so #16 will alert.

## 7. The `query_sdr` tool (ADR-0008)

| Argument | Meaning |
| --- | --- |
| `mode` | `list` (newest first), `count_by_jasc`, `count_by_part`, `count_by_month` |
| `aircraft` | model or family, normalised as in §4. It matches `model` **or** `model_family` (case-insensitive) |
| `jasc` | 2–4 digits, used as a prefix: "32" means all landing gear |
| `text` | narrative words, as a Postgres `websearch_to_tsquery`. Words are ANDed, and `"quoted phrases"` work |
| `part` | part name contains. `%` and `_` are taken literally |
| `date_from`, `date_to` | difficulty date range |
| `limit` | 1–20, default 10: reports in `list`, groups in the count modes. `count_by_month` returns every month in range, up to 60 |

At least one of `aircraft`, `jasc`, `text` and `part` is required, so the model can't ask for the whole table.

**Safety:**
- The SQL is built only from fixed fragments with bound parameters. Nothing the model writes is spliced into the query (tested with injection strings in every text argument).
- It runs as `tools_ro`, whose sessions are read-only and which has no grants on the base tables (both layers tested).
- It reads through the `tools_sdr` view, which hides tombstoned reports.
- `statement_timeout` is 5 s.
- Output is capped at `MAX_TOOL_CHARS` (about 2k tokens), and each narrative is cut to 600 characters.

**Output:**
- A header with the filters used and the total count and date range.
- Then either rows (`[sdr:ID] date · make model · JASC · part (condition) · stage`, then the narrative) or group counts.
- A fixed note: *"Counts are what operators chose to report, not failure rates."* SDR volume follows fleet size and reporting habits, so the model must not read a count as a reliability figure.
- With no match on an aircraft, a hint to try the family.

**Citable:** reports returned by `list` become evidence under `sdr:<id>`, so diagnose and draft can cite them, and quotes are checked the same way as document quotes. The diagnose prompt says what an SDR proves: **"a known pattern on this type, not a procedure, and not proof of the cause on this aircraft."**

**Prompts:** `agent_act.md` describes the tool and when to use it, and `agent_diagnose.md` gains rule 6. `PROMPT_VERSION` changes automatically because it hashes the prompts. `GRAPH_VERSION` stays "2": nodes and edges are unchanged, and runs paused under the old prompt resume normally.

## 8. Live results (local, 2026-11-01)

| Measure | Result |
| --- | --- |
| First load, 2024–2026 | **183,592 reports in 19 s**: 0 rows dropped, 0 quarantined, $0.00 |
| Immediate re-sync | 183,592 unchanged, 0 written, 0 tombstoned |
| `list`, "Boeing 737-800" + "chip detector" | 24 ms, 12 reports (2024-01-31 to 2026-10-03), 10 returned and citable |
| `count_by_part`, CRJ900, JASC 32 | 50 ms. 146 reports; top parts: selector valve 19, sensor 19, warning message 11 |
| `count_by_jasc`, A320 family | 60 ms. 9,149 reports; JASC 3350 (999), 2120 (825), 5310 (669) |
| `count_by_month`, 737-8, "engine" | 23 ms, 262 reports over 34 months |
| `list`, E175 + "bleed leak" | 5 ms, 21 reports |

**One observation:** the narrative search ANDs words, not phrases. "chip detector" also matched a report that mentions "chip" and "detector" separately. The model can quote phrases, and the tool description doesn't promise more.

## 9. Tests

`tests/test_sdr_models.py` (48):
- 32 real designations (the MAX trap, CRJ variants, A220, freighters, DC-9-83);
- unknown models keep their raw value;
- make expansion;
- 14 query-side terms, including the fleet table's own type strings.

`tests/test_ingest_sdr.py` (29), on real Postgres with a fake FAA server:
- parsing and cleaning; the N-number isn't kept;
- the row hash changes with content and year, and only then;
- **seven ways a file is rejected whole**: block page, missing column, renamed column, bad encoding, empty file, too many empty makes, too many bad dates;
- a few bad rows dropped without rejecting the file;
- insert, then unchanged;
- amended reports updated and withdrawn ones tombstoned **in their year only**, then revived;
- a report moving years stays live;
- **a truncated file is quarantined and tombstones nothing**;
- one missing year fails the run but keeps the others;
- a bad header doesn't touch stored reports;
- `query_sdr`:
  - model vs family matching (737-800 doesn't return 737-8 MAX reports);
  - citable evidence with quote checks;
  - all three count modes;
  - `%` and `_` treated literally;
  - injection strings;
  - tombstoned reports hidden;
  - five kinds of invalid arguments returned to the model;
  - the no-match hint;
  - the output cap;
  - `tools_ro` can't read the base table or write anything.

**Mutation checks**, all confirmed to fail a test:
- removing the truncation check;
- tombstoning across all years instead of the file's year;
- removing the `%`/`_` escaping in the part filter.

## 10. Known limits and follow-ups

| Item | Where |
| --- | --- |
| JASC codes are shown without their titles, so the model has to know that 3242 means brakes. Add the FAA JASC code table as a lookup | next SDR change, or Phase 3 if evals show the model misreading codes |
| Stage-of-operation and how-discovered codes are stored as codes (IN, CR, V…), not decoded | same |
| **UNVERIFIED:** how well the live agent chooses `query_sdr` arguments. ADR-0008's spike had both shortlisted models fill the schema 16/16, but no live run has used the final tool yet (no spend in this slice) | the next live agent run; Phase 3 evals |
| Backfill before 2024 (files exist back to 1980, about 1.5M rows) | Phase 8 load tests |
| Alerts on `sync_sdr` failure, quarantine and a tombstone spike; the nightly header contract check | #16 |
| Aliases are upserted but never removed | harmless; revisit if the table grows |
