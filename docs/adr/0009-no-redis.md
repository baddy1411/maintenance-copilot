# ADR-0009: No Redis; rate limits, spend counters and caches live in Postgres

Status: Proposed · 2026-10-29 · new in DESIGN v2 (findings C11 and O9, raised independently by the cost and operations reviews)

## Context
v1 kept Redis (ElastiCache on AWS) for rate limits and an exact-match answer cache. ADR-0001 had already moved the job queue to Postgres, and the semantic cache is planned for pgvector. Expected load is about 3–10 limit checks per second at 50 concurrent users.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Redis / ElastiCache | Fast atomic counters; TTLs built in | $0.4–3 per day on AWS (ASSUMED; the serverless floor is the higher figure); another Compose service, Terraform module and failure domain; spend counters still have to reconcile with the Postgres ledger; fail-open or fail-closed behaviour has to be defined |
| **Postgres tables** | `INSERT … ON CONFLICT DO UPDATE … RETURNING` is atomic across replicas; counters live next to the `llm_calls` ledger; nothing new to run | Adds write load to the main database (small at this rate); TTL cleanup is a scheduled delete |

## Decision
Use a `limits` table (subject, window, kind → counter) for rate and spend limits, and a Postgres table for the exact-match cache, keyed by question hash, access scope and `corpus_version`. The Phase 4 semantic cache uses pgvector.

## Consequences
- One fewer service everywhere. Spend limits **fail closed** if the database is down, and so does everything else.
- **Revisit if** limit and cache queries exceed 10 % of database time in the Phase 8 load test, or limit-check p95 exceeds 5 ms.
