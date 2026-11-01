# Approvals and the work-order write

How a supervisor's decision turns a paused agent run into exactly one work order, or into nothing, and why nothing else can. It's the detailed companion to DESIGN §6.3 and security review findings S1, S2 and S8.

Code: `app/api/runs.py` (decision endpoint, approval queue), `app/agent/approval.py` (write and reject), `app/agent/graph.py` (nodes after the interrupt), `app/agent/runner.py` (resume, expiry, deploy check), `app/audit.py` (hash chain). Tests: `tests/test_approval_property.py`, `test_approvals.py`, `test_audit.py`, plus the stream regression in `test_api_runs.py`. Dates: 2026-11-01/02.

## 1. The rule
> A work order exists **if and only if** an `approvals` row exists for the run, its action is approve or edit, its `draft_sha256` matches the draft in the run's checkpoint, and its approver isn't the run's creator.

DESIGN §2 makes "writes without a persisted, authorised approval" a hard invariant at zero. Each condition is checked **more than once**, by different code:

| Condition | Decision endpoint | Write node (same transaction as the insert) | Database |
| --- | --- | --- | --- |
| Approver is a real, allowed user | role `supervisor` or `admin`; identity from the principal, never the body | re-reads `approvals.approver_id` | — |
| Not the creator | 403 | `ApprovalInvalid` | `work_orders CHECK (created_by <> approved_by)` |
| Bound to the draft the supervisor saw | 409 on `draft_sha256` mismatch | compares the row's hash with the checkpoint's | — |
| One decision per run | row lock + compare-and-set on `status = 'pending_approval'` | — | `approvals.run_id UNIQUE` |
| One work order per run | — | `INSERT … ON CONFLICT (run_id) DO NOTHING` | `work_orders.run_id UNIQUE` |
| Only approve/edit writes | — | `ApprovalInvalid` for any other action | — |

## 2. Flow

```mermaid
sequenceDiagram
    participant S as Supervisor
    participant A as API
    participant DB as Postgres
    participant W as Worker
    S->>A: GET /approvals → drafts + draft_sha256
    S->>A: POST /runs/{id}/decision {action, draft_sha256, edited_draft?, reason?}
    A->>DB: BEGIN; SELECT run FOR UPDATE; checks
    A->>DB: UPDATE runs SET status='decided' WHERE status='pending_approval' AND … (compare-and-set)
    A->>DB: INSERT approvals; audit.append("decision"); run_event; COMMIT
    A->>DB: defer run_agent (lock run:{id})
    A-->>S: 202 {approval_id}
    W->>DB: claim (decided → running); load approvals row
    W->>W: graph resumes with Command(resume=row) → approval → write | reject
    W->>DB: BEGIN; re-read approvals FOR UPDATE; check; INSERT work_order; audit.append; status=written; COMMIT
    W->>DB: events "written", status "written"
```

## 3. The decision endpoint
`POST /runs/{run_id}/decision`, for supervisors and admins:

| Situation | Response |
| --- | --- |
| Technician | 403 |
| Unknown run | 404 |
| Not `pending_approval` (already decided, written, rejected, expired…) | 409 "run is …" |
| Paused more than 7 days | 409 "approval window has passed" |
| Your own run | 403 |
| `draft_sha256` doesn't match the current draft | 409 "the draft has changed since you reviewed it" |
| Run on a graph version this release doesn't serve | 409 |
| `reject` without a reason, `edit` without a draft | 422 |
| An edit changes the registration, or adds a citation that wasn't in the draft | 422 |
| Otherwise | 202 `{run_id, status: decided, approval_id}`, and the resume job is queued |

There's **no approver field**. A body that includes `approver_id` has it ignored, and the stored approver is the caller (tested). `GET /approvals` lists paused runs oldest first, each with its draft, the `draft_sha256` to quote, and `own_run` so the UI can disable a supervisor's own runs.

**Edits** may change the title, priority, summary and steps, and may *drop* citations, but not add any. Every citation in a draft was checked against a passage the agent retrieved, and the supervisor's edit can't add unchecked ones. Kept citations are stored as the original, fully resolved citation (page label, AD number), not the bare `{chunk_id, quote}` the edit sent. `draft.checks` gets "edited by supervisor".

## 4. The worker side
- The runner now claims runs in `decided` as well. If the checkpoint is paused at `approval` and an approvals row exists, the graph resumes with `Command(resume={approval_id, action})` **built from the row**. A paused run without a row just re-pauses (crash recovery).
- `approval → write | reject` is routing only. **Both nodes re-read and re-check the approvals row** in their own transaction, so a wrong or forged resume value can't produce a work order.
- **Write is idempotent across a crash.** If a worker dies after the write commits but before the checkpoint records it, the re-run returns the same work order (`ON CONFLICT`), and the audit entry is appended only when the insert is new.
- An `ApprovalInvalid` (a write the row doesn't authorise) fails the run with the reason. Nothing is written, and retrying can't change that.

## 5. Evidence

### 5.1 Property test: every combination (`tests/test_approval_property.py`)
- 12 combinations: approvals row with action {approve, edit, reject} × hash {matching, stale} × approver {creator, someone else}. Plus no row at all, for 13 real runs through the graph.
- The rows are written **straight into the database, bypassing the endpoint**, as a bug or tampering would. This tests the write node's own defence, not the endpoint's.
- A separate test resumes the graph **directly** with a forged `Command(resume={"action": "approve"})` and no row. It raises `ApprovalInvalid` and writes nothing.
- A structural check: the graph's only edges into `write` and `reject` come from `approval`.

**Mutation check:** with the write node's hash check removed, exactly the 3 cases where it is the *only* defence fail (stale hash, non-creator approver: approve, edit, reject). Every other case is still caught by another check.

### 5.2 Endpoint and end-to-end tests (`tests/test_approvals.py`, 17)
Roles; the queue; own run; approver from the principal, not the body; stale hash; one decision; **concurrent approve vs reject** (exactly one 202, one 409, one approvals row, one audit entry, one job); reject/edit validation; edit rules; expiry; unserved version; approve, edit and reject through the worker; the write's idempotency; a **v1 checkpoint resumed under graph v2**; and that the hash a supervisor quotes is the hash of the exact draft they were shown.

### 5.3 Live
The two real runs from #10, paused under **graph v1**, decided under v2 by supervisor `sup-anna`:

| Run | Decision | Result |
| --- | --- | --- |
| MX-001, metal in engine oil (urgent) | approve | 202; resumed (attempt 2); **work order written**: created by `badrish-demo`, approved by `sup-anna` |
| MX-007, spongy brake after wheel change (urgent) | reject, "Brake work needs the wheel and brake change paperwork checked first" | 202; resumed; **rejected**, no work order |
| MX-001 again, by `sup-ben` | approve | **409** "run is running, not pending approval" |

Audit chain afterwards: `decision, decision, work_order_written, work_order_rejected`, **verified intact**. No model calls: write and reject don't use the model.

## 6. The audit log (`app/audit.py`)
- `row_hash = sha256(prev_hash + canonical JSON of (ts in UTC, actor, action, run_id, payload))`. `verify()` walks the chain and returns the first broken row.
- `append()` runs **inside the caller's transaction**, so an audit row commits or rolls back with the change it records (tested).
- **A transaction-level advisory lock** serialises appends. Without it, concurrent appends forked the chain in **5 of 5 runs** (mutation check); with it, 10 concurrent appends verify.
- **Timestamps are hashed in UTC.** Postgres returns `timestamptz` in the session's zone, so the same instant can read back as "+05:30". A test verifies from an Asia/Kolkata session; without the UTC normalisation it fails.
- Tampering is detected: an edited row fails at that row, and a deleted row fails at the next one (tested as the table owner, since service roles can't UPDATE or DELETE at all).

## 7. Expiry and deploys
- **`expire_approvals`** runs daily at 03:30 UTC. Runs paused more than 7 days become `expired`, each with an audit entry (`run_expired`, actor `system`) and an event. The decision endpoint refuses them even before the job runs.
- **`copilot check-deploy`** is the pre-deploy gate (DESIGN §6.3.1). It lists live runs on graph versions the release doesn't serve, and exits 1 if there are any. `--expire` expires them with audit entries instead.
- **Graph v2 serves v1** (`SERVED_GRAPH_VERSIONS = {"1", "2"}`). v2 only added nodes *after* the interrupt, so a v1 run paused at `approval` resumes correctly. Shown live and in a test.

## 8. A bug found live: streams ending before the last event
The approve stream ended right after the `decision` event, without `written` or the final status. Why: the write node commits `runs.status = 'written'` a moment before the runner records its final events. The stream ended on that status column, so a client could miss the work-order id. Streams now end on the **event log**: when the last event is a status event for a finished state, with a 5 s grace for runs that ended without one. A regression test reproduces the race (the final events arrive 0.8 s after the status flips), and it failed before the fix.

## 9. Follow-ups

| Item | Where |
| --- | --- |
| JWT and real roles (the dev principal takes `X-Dev-Role` until then, local only) | #13 |
| Daily `audit_chain_verify` job with an alert | #16 |
| Supervisor UI: queue, diff of edits, reject reasons | #14 |
| Work-order lifecycle after creation (open → done) | out of scope (`SCOPE.md`) |
