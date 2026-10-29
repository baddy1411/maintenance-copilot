# ADR-0001: Postgres-backed job queue

Status: Proposed · 2026-10-29

## Context
Ingestion needs background jobs: a daily AD sync, weekly SDR syncs, admin PDF uploads, and re-embedding. Volume is small: about 10 ADs per day, 1 to 3 SDR files per week, and occasional PDFs. A first-time backfill is larger (about 10k ADs and 1.5M SDR rows) but runs once. `PLAN.md` suggested Redis locally and SQS on AWS.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Redis queue locally (arq) + SQS on AWS | Matches the plan; SQS scales without limit | **Two backends**, so local tests don't exercise the AWS path; adds a managed service; jobs aren't transactional with the data they write |
| SQS everywhere (LocalStack locally) | One API | LocalStack in Compose is heavy; still no transactional enqueue |
| **Postgres queue (`procrastinate` 3.10, MIT)** | One backend everywhere; enqueue in the same transaction as the data; periodic tasks and retries are built in; no extra service | Throughput is limited by Postgres (far above our needs); it ties the queue to the database's availability |
| Celery | Mature | Heavy for this workload; needs a broker anyway |

## Decision
Use **procrastinate on the main Postgres**, with its own schema. Workers scale as a separate ECS service (or Compose service). Its LISTEN/NOTIFY wakeups are UNVERIFIED from the docs, so polling latency gets measured in Phase 2.

## Consequences
- **Agent runs also use this queue** (ADR-0004, revised in DESIGN v2), so pickup latency now affects users. The Phase 2 measurement must show p95 pickup ≤ 1 s.
- The same code and tests run locally and on AWS.
- If ingestion ever reaches thousands of jobs per second, move to SQS behind the same `enqueue()` interface. The trigger to revisit is queue latency p95 above 60 s or database CPU above 60 % caused by job polling.
- This drops the Phase 8 "workers scaled on queue depth" requirement on SQS. Workers scale on the procrastinate queue-depth metric instead, which we export to CloudWatch or Prometheus.
