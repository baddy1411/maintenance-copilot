# ADR-0004: Agent as a library, with runs executed as worker jobs

Status: Proposed · 2026-10-29 · **revised in DESIGN v2** after the cost and scale and operations reviews (findings C9 and O3)

## Context
`PLAN.md` lists a separate "LangGraph agent service". A run streams its steps to the user, takes about 35–45 s, pauses for supervisor approval (which can take hours or days), and then resumes. v1 ran the graph inside the API request handler that held the SSE stream. The reviews found three problems with that:
- A deploy or scale-in sends SIGTERM, and the default 30 s ECS stop timeout is shorter than a run, so runs are killed mid-graph.
- Nothing resumes a run whose process died.
- A client disconnect either keeps spending or orphans the run.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Separate agent service | Scales independently | Extra hop, auth between services and another deployment; the work is I/O-bound, so there's no benefit at our scale |
| Library inside the API request (v1) | Simplest; lowest latency | Runs are tied to the request's lifetime: killed on deploy, orphaned on disconnect, nothing to reap them |
| **Library executed by the worker as a procrastinate job; API streams from `run_events`** | Runs survive deploys and disconnects; the reaper re-enqueues stalled runs; clients re-attach; the worker scales on queue depth; one queue (ADR-0001) | Adds job-pickup latency (LISTEN/NOTIFY, UNVERIFIED), plus a `run_events` table and its polling or NOTIFY |
| LangGraph Platform | Managed runs | Vendor dependency and cost |

## Decision
- The graph lives in `app/agent`. **`POST /runs` inserts a `runs` row and enqueues a job.** The worker executes the graph with **`langgraph-checkpoint-postgres` 3.2.0**, writes progress to `run_events` and updates `runs.heartbeat_at` at every step. The API streams `run_events` over SSE.
- At the interrupt, the run stops with `status = pending_approval`. The decision endpoint records the approval (DESIGN §6.3) and enqueues a resume job.
- **Workers drain on SIGTERM.** They finish the current step and stop, with ECS `stopTimeout` = 120 s. `reap_runs` re-enqueues runs that have had no heartbeat for 5 minutes, at most twice.
- Q&A stays in the API request, because it is short and has nothing to resume.

## Consequences
- Job-pickup latency adds to agent latency. The Phase 2 measurement must show p95 pickup ≤ 1 s, or else poll the queue more often.
- Cross-process resume is now the normal path rather than an edge case, so the Phase 2 two-process kill-and-resume test is mandatory (`SPIKE.md` §3 marks it UNVERIFIED).
- Breaking graph changes need run versioning (DESIGN §6.3.1).
