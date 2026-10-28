# Scope

Status: **DRAFT, awaiting approval (Phase 0 gate)**. Last updated 2026-10-28.

## Domain decision

**Aviation maintenance.** It was chosen because it has large, *live*, licence-clean public data on both sides, the documents and the fault history. (The first draft used industrial pumps and compressors with two DOE sourcebooks. That was too small and had no real fault data, so it was dropped.)

- **Documents:** FAA Airworthiness Directives (thousands, published daily via the Federal Register API), the FAA Aviation Maintenance Technician Handbooks (General, Airframe, Powerplant) and AC 43.13-1B, *Acceptable Methods, Techniques and Practices: Aircraft Inspection and Repair* (about 2,900 pages together).
- **Fault history:** FAA Service Difficulty Reports (SDRs), real maintenance discrepancy reports filed by operators and repair stations. There are 183k rows for 2024–2026 alone, and the files go back to 1980.

See `docs/DATA.md` for sources, licences and volumes.

## Problem

Maintenance technicians troubleshoot using thousands of pages of guidance, a constant stream of new Airworthiness Directives and fleet-wide fault history that nobody can read in full. Finding the applicable AD, the right inspection practice and how often a fault has occurred on the same type takes time and is error-prone. Maintenance Copilot answers with citations, reasons over fault history and drafts work orders that a human approves.

## Users

| User | Needs | Can do |
| --- | --- | --- |
| **Technician** (AMT / mechanic) | Fast, cited answers; applicable ADs for an aircraft or part; what the fleet-wide SDR history says about a symptom; a drafted work order | Ask questions, run the troubleshooting agent, submit a draft work order |
| **Supervisor / inspector** | Control over what gets written; visibility of what the agent did and why | Everything a technician can, plus approve, edit or reject work orders and read the audit log |
| **Admin** (operator) | Keep the corpus current; manage users and config | Trigger and monitor ingestion, manage roles and settings |

## Core paths (in scope)

1. **Cited question answering** over handbooks, AC 43.13-1B and ADs. Every factual sentence cites a chunk (document, page or AD number, section). If the sources don't support an answer, it says so instead of guessing.
2. **Live document ingestion.** A scheduled job pulls new ADs from the Federal Register API every day and new SDR rows from the FAA yearly files. Admins can also upload PDFs. Ingestion is incremental (keyed by document number or content hash) and idempotent. A document is searchable only after it has been ingested successfully.
3. **Troubleshoot and draft a work order (agent).** A technician describes a symptom on an aircraft in our (fictional) fleet. The agent searches the guidance, finds applicable ADs, queries SDR history for the same make, model and ATA chapter, proposes a diagnosis with citations and drafts a work order. **Nothing is written until a supervisor approves** (a human-in-the-loop interrupt). Drafts are idempotent.

## Out of scope

- Signing off airworthiness, return-to-service or any regulatory record. The copilot is advisory. Certificated people make and sign every decision.
- Writing to real MRO or CMMS systems (AMOS, TRAX, Maximo and others). We write to our own `work_orders` table, and an adapter is a documented extension point only.
- OEM manuals (AMM, CMM, IPC). They are proprietary and not licensable. The system tells users when the answer needs the OEM manual.
- Determining legal AD applicability (serial-number effectivity, compliance tracking). We surface likely-relevant ADs with citations and do not certify compliance.
- Real-time aircraft telemetry, predictive maintenance models, image or diagram understanding, mobile/offline, multi-tenant SaaS, non-English content, and a self-hosted LLM (any OpenAI-compatible endpoint still works through config).

## Safety boundaries

- The copilot never advises deferring or ignoring an AD, exceeding airworthiness limitations, bypassing required inspections, or signing off work that wasn't done. Such requests are refused and flagged (built in Phase 5; the requirement is recorded now).
- Answers always state that the approved data (OEM manual or AD) governs.
- Documents and questions are sent to a third-party LLM API (OpenRouter and its upstream providers). This is stated in the system card and security docs.

## Success criteria (made numeric in Phase 1 `DESIGN.md`)

- Answers are grounded and correctly cited on a golden set (Phase 3).
- The agent picks the right tools and drafts valid work orders, and it never writes without approval.
- The daily AD sync runs unattended. A new AD is searchable within 24 hours of publication.
- p95 latency and cost per query stay within budgets set in the design. Retrieval stays within targets at full corpus size (all SDR years from 1980).

## Open questions for approval

1. Corpus size for Phase 2: start with ADs from 2020 onward and SDRs from 2024 onward (about 2,500 ADs and 183k SDRs), then backfill to full history in Phase 8 load testing?
2. The fleet is fictional (about 30 aircraft of real types such as B737-800, A320 and CRJ-900, with fake registrations). Acceptable?
