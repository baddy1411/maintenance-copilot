# Data

Status: **DRAFT, awaiting approval (Phase 0 gate)**. Last updated 2026-10-28.

The rules: every source is real, public, licence-clean and fetched from an official endpoint that allows scripted access. Large and **live** (continuously updated) beats small and static. Only the fleet and work orders are synthetic.

## Sources

| ID | What | Volume (checked 2026-10-28) | Live? | Access | Licence |
| --- | --- | --- | --- | --- | --- |
| `ad` | **FAA Airworthiness Directives**, final rules: mandatory inspections and fixes per aircraft, engine or part type | 2020–2026: **2,553** ADs (396 / 491 / 424 / 348 / 283 / 318 / 293 per year). Full history: about 10,000+ | **Yes.** New ADs are published most business days | Federal Register API: `GET https://www.federalregister.gov/api/v1/documents.json` filtered by agency `federal-aviation-administration`, type `RULE`, term `"airworthiness directives"`, then `raw_text_url` for full text. No key needed. | US federal government work, public domain (17 U.S.C. §105) |
| `sdr` | **FAA Service Difficulty Reports**: real discrepancy reports from operators and repair stations. 80 columns, including `JASCCode` (ATA chapter), aircraft, engine and part make/model/number, stage of operation, how discovered, free-text `Discrepancy` | 2024: **66,080** rows; 2025: **67,652**; 2026 to date: **49,178**. Median discrepancy text is about 200 chars. Files exist for every year 1980–2026. Top makes: Boeing, Airbus, Bombardier (CNDAIR), Embraer, Cessna | **Yes.** The current-year file keeps growing, and back-years get late reports | `GET https://external.apic4e.faa.gov/sdrs/retrieve/SDR-{year}.csv` (a direct CSV link from faa.gov/av-info/download_SDR) | FAA public data, US government work. Contains operator codes and aircraft registrations (business data, no personal data) |
| `faa-amt-general` | *Aviation Maintenance Technician Handbook: General*, FAA-H-8083-30B | 677 pages, 92.5 MB | Revised every few years | Direct PDF from faa.gov | US government work. No copyright notice found in the front or back matter |
| `faa-amt-airframe` | *AMT Handbook: Airframe*, FAA-H-8083-31B | 1,052 pages, 112 MB | Same | Direct PDF | Same |
| `faa-amt-powerplant` | *AMT Handbook: Powerplant*, FAA-H-8083-32B | 500 pages, 215 MB | Same | Direct PDF | Same |
| `faa-ac-43-13-1b` | AC 43.13-1B with Change 1, *Acceptable Methods, Techniques and Practices: Aircraft Inspection and Repair* | 646 pages, 21 MB | Static | Direct PDF | Same |

Totals for the spike: about **2,875 handbook pages + 2,553 ADs + 182,910 SDRs**. Backfilling SDRs to 1980 and ADs to the start of the API gives roughly an order of magnitude more, which is what the Phase 8 load and scale tests will use.

### Licence notes

- US federal works have no copyright in the US. We searched the first 30 and last 5 pages of each handbook for "copyright", "©", "all rights reserved" and "public domain" and found nothing. Some handbook figures may be credited to third parties. We use the text only, and we cite by page.
- The ADs often reference **OEM service bulletins**, which are proprietary and not included. Answers that depend on an SB must say so.

### Access notes (verified)

- faa.gov **HTML** pages and nhtsa.gov return HTTP 403 to scripted fetches. We don't work around that (no fake browser headers). The FAA PDF and CSV files and the Federal Register API answer plain scripted requests, and those are all we need. The SDR file links were read once from the FAA page in a normal browser.
- The fetcher sends an honest User-Agent and waits 0.1 s between AD requests.

## Synthetic data (generated)

| Table | Purpose | Notes |
| --- | --- | --- |
| `fleet` | About 30 fictional aircraft of **real types** that appear in the SDR data (for example B737-800, A320-200, CRJ-900, ERJ-175, Cessna 208) | Fake registrations in a reserved pattern (`MX-001`…) so they can never collide with real N-numbers. Each has hours, cycles, engine type and in-service date |
| `work_orders` | What the agent drafts and supervisors approve | Seeded with about 300 historical rows. Idempotency key unique |
| `users` | Technician, supervisor and admin accounts | Obviously fake names (`tech_01`) |

The agent links the fictional fleet to real data by **type**: "ADs for B737-800 main landing gear" and "SDRs on B737-800, JASC 3230, last 24 months".

## Handling and reproducibility

- `spikes/fetch_data.py` (later `scripts/ingest/`) downloads everything into a git-ignored `data/` folder. We commit URLs, volumes and hashes, never the data.
- AD ingestion is incremental by `document_number`, SDR ingestion by year file plus a content hash per row, and PDFs by SHA-256.

## Data that leaves the machine

Document chunks, SDR rows retrieved for a question, user questions and agent messages are sent to **OpenRouter** and on to the upstream model provider. Embedding requests send chunk text. Requests set `provider.data_collection: "deny"` to exclude providers that store or train on data (OpenRouter provider-routing docs, checked 2026-10-28). Phase 5 decides whether to also require zero data retention (`provider.zdr: true`).
