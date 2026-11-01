"""Agent runs over HTTP (DESIGN §6.3, ADR-0004). The API never executes a run: it creates the row, defers the
job, and streams the run's events from Postgres. Any API replica can serve any run's stream.

    POST /runs                {registration, symptom}  + Idempotency-Key   -> 202 {run_id, status, events_url}
    GET  /runs/{run_id}                                                      -> status, versions, draft
    GET  /runs/{run_id}/events?after=N   (or Last-Event-ID)                  -> SSE, one event per run_event
    POST /runs/{run_id}/decision  {action, draft_sha256, edited_draft?, reason?}   supervisors -> 202
    GET  /approvals                                                          supervisors -> pending runs
"""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field, model_validator

from app import audit
from app.agent.graph import GRAPH_VERSION, PROMPT_VERSION, SERVED_GRAPH_VERSIONS, STATE_SCHEMA_VERSION
from app.agent.schemas import REGISTRATION, WorkOrderDraft
from app.api.auth import Principal, can_decide, dev_principal
from app.jobs import enqueue_run

router = APIRouter()
STREAM_POLL_S = 0.3
STREAM_MAX_S = 600.0  # after this the stream ends; the client re-attaches with Last-Event-ID
FINAL_EVENT_GRACE_S = 5.0  # a finished run with no final status event still ends the stream after this
END_STATES = {"pending_approval", "decided", "written", "rejected", "failed", "expired"}


class RunRequest(BaseModel):
    registration: str = Field(pattern=REGISTRATION)
    symptom: str = Field(min_length=5, max_length=2000)


def request_hash(body: RunRequest) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


@router.post("/runs", status_code=202)
async def create_run(
    body: RunRequest, request: Request, response: Response, principal: Annotated[Principal, Depends(dev_principal)],
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> dict[str, Any]:  # fmt: skip
    st = request.app.state
    async with st.ro.connection() as conn:  # nothing is spent on an aircraft that isn't ours
        cur = await conn.execute("SELECT 1 FROM tools_fleet WHERE registration = %s", (body.registration,))
        if await cur.fetchone() is None:
            raise HTTPException(404, f"{body.registration} is not in the fleet")
    key, sha = idempotency_key or f"auto:{uuid4()}", request_hash(body)
    async with st.rw.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO runs (created_by, registration, symptom, client_key, request_sha256, graph_version, "
            "prompt_version, config_version, state_schema_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (created_by, client_key) DO NOTHING RETURNING run_id",
            (principal.user_id, body.registration, body.symptom, key, sha, GRAPH_VERSION, PROMPT_VERSION,
             st.settings.config_version(), STATE_SCHEMA_VERSION),
        )  # fmt: skip
        row = await cur.fetchone()
        if row is None:  # a retry of an earlier request with the same key
            cur = await conn.execute(
                "SELECT run_id, request_sha256, status FROM runs WHERE created_by = %s AND client_key = %s",
                (principal.user_id, key),
            )
            existing = await cur.fetchone()
            assert existing is not None
            if existing[1] != sha:
                raise HTTPException(409, "Idempotency-Key was already used for a different request")
            response.status_code = 200
            return {"run_id": str(existing[0]), "status": existing[2], "events_url": f"/runs/{existing[0]}/events"}
        run_id = UUID(str(row[0]))
        await conn.execute("INSERT INTO run_events (run_id, seq, kind, payload) VALUES (%s, 1, 'status', %s)",
                           (run_id, json.dumps({"status": "queued"})))  # fmt: skip
    # If deferring fails after the insert, the run stays queued and reap_runs enqueues it later.
    await enqueue_run(st.jobs, run_id)
    return {"run_id": str(run_id), "status": "queued", "events_url": f"/runs/{run_id}/events"}


async def own_run(request: Request, run_id: UUID, principal: Principal) -> tuple[Any, ...]:
    """The creator, supervisors and admins can read a run (DESIGN §7.3). For anyone else it's a 404, so its
    existence isn't revealed (security review S5)."""
    async with request.app.state.rw.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id, registration, symptom, status, graph_version, prompt_version, config_version, "
            "state_schema_version, draft_sha256, cost, attempts, created_at, updated_at "
            "FROM runs WHERE run_id = %s AND (created_by = %s OR %s)",
            (run_id, principal.user_id, can_decide(principal)),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "run not found")
    return tuple(row)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: UUID, request: Request, principal: Annotated[Principal, Depends(dev_principal)]
) -> dict[str, Any]:
    r = await own_run(request, run_id, principal)
    keys = ("run_id", "registration", "symptom", "status", "graph_version", "prompt_version", "config_version",
            "state_schema_version", "draft_sha256", "cost", "attempts", "created_at", "updated_at")  # fmt: skip
    out = {k: (str(v) if k in ("run_id", "created_at", "updated_at") else v) for k, v in zip(keys, r, strict=True)}
    out["cost"] = float(out["cost"])
    async with request.app.state.rw.connection() as conn:
        cur = await conn.execute(
            "SELECT payload FROM run_events WHERE run_id = %s AND kind = 'draft' ORDER BY seq DESC LIMIT 1", (run_id,)
        )
        draft = await cur.fetchone()
    out["draft"] = draft[0]["draft"] if draft else None
    return out


async def owned_run_id(run_id: UUID, request: Request, principal: Annotated[Principal, Depends(dev_principal)]) -> UUID:
    """As a dependency, the 404 is raised before a streaming response starts. Inside the SSE generator the
    status line is already sent, so it couldn't be."""
    await own_run(request, run_id, principal)
    return run_id


@router.get("/runs/{run_id}/events", response_class=EventSourceResponse)
async def run_events(
    run_id: Annotated[UUID, Depends(owned_run_id)], request: Request,
    after: Annotated[int, Query(ge=0)] = 0, last_event_id: Annotated[str | None, Header()] = None,
) -> AsyncIterator[ServerSentEvent]:  # fmt: skip
    """Replays events after `after` (or Last-Event-ID), then tails new ones until the run reaches a state
    that needs no more work from the worker. Events come from Postgres, so this works on any replica, after
    a disconnect, and after a worker restart."""
    seq = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    deadline = time.monotonic() + STREAM_MAX_S
    last_was_final = False  # the last event streamed is a status event for a state needing no more work
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        async with request.app.state.rw.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, kind, payload FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT 200",
                (run_id, seq),
            )
            rows = await cur.fetchall()
            cur = await conn.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
            status_row = await cur.fetchone()
        for s_, kind, payload in rows:
            seq = s_
            last_was_final = kind == "status" and payload.get("status") in END_STATES
            yield ServerSentEvent(id=str(s_), event=kind, data=payload)
        if rows:
            quiet_since = None
            continue
        # End on the event log, not the runs row: a write commits runs.status a moment before the runner
        # records its final events (found live in #12). Every path that finishes a run records a final status
        # event; the grace period covers runs that ended without one.
        status = status_row[0] if status_row else None
        quiet_since = quiet_since or time.monotonic()
        if last_was_final or (status in END_STATES and time.monotonic() - quiet_since > FINAL_EVENT_GRACE_S):
            yield ServerSentEvent(event="end", data={"status": status})
            return
        await asyncio.sleep(STREAM_POLL_S)


# ---- approvals (#12, DESIGN §6.3) ------------------------------------------------------------------------

APPROVAL_WINDOW = "7 days"  # expire_approvals marks older pending runs expired; the decision refuses them anyway


class DecisionRequest(BaseModel):
    action: Literal["approve", "edit", "reject"]
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="The hash of the draft you reviewed")
    edited_draft: WorkOrderDraft | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def complete(self) -> "DecisionRequest":
        if self.action == "edit" and self.edited_draft is None:
            raise ValueError("edit needs edited_draft")
        if self.action == "reject" and not self.reason:
            raise ValueError("reject needs a reason")
        return self


async def latest_draft(conn: Any, run_id: UUID) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT payload FROM run_events WHERE run_id = %s AND kind = 'draft' ORDER BY seq DESC LIMIT 1", (run_id,)
    )
    row = await cur.fetchone()
    return dict(row[0]["draft"]) if row else None


def checked_edit(edit: WorkOrderDraft, original: dict[str, Any], registration: str) -> dict[str, Any]:
    """An edit keeps the run's aircraft, and may only keep or drop the original's citations: those were
    checked against retrieved passages, and an edit can't add unchecked ones (DESIGN §6.3)."""
    if edit.registration != registration:
        raise HTTPException(422, "an edit cannot change the registration")
    known = {(c["chunk_id"], c["quote"]): c for c in original["citations"]}
    cites = []
    for c in edit.citations:
        if (c.chunk_id, c.quote) not in known:
            raise HTTPException(422, f"citation {c.chunk_id!r} is not one of the draft's checked citations")
        cites.append(known[(c.chunk_id, c.quote)])
    return {**edit.model_dump(), "citations": cites, "checks": [*original.get("checks", []), "edited by supervisor"],
            "evidence_incomplete": original.get("evidence_incomplete", False)}  # fmt: skip


@router.post("/runs/{run_id}/decision", status_code=202)
async def decide(
    run_id: UUID, body: DecisionRequest, request: Request, principal: Annotated[Principal, Depends(dev_principal)]
) -> dict[str, Any]:
    """The only way a paused run continues (DESIGN §6.3). The approver is the authenticated principal: there
    is no approver field in the body. One transaction: lock the run, check, compare-and-set to 'decided',
    insert the approvals row, append the audit entry, record the event. Then enqueue the resume job."""
    if not can_decide(principal):
        raise HTTPException(403, "only supervisors can decide on runs")
    st = request.app.state
    async with st.rw.connection() as conn, conn.transaction():
        cur = await conn.execute(
            f"SELECT created_by, status, draft_sha256, graph_version, registration, "  # noqa: S608
            f"updated_at < now() - interval '{APPROVAL_WINDOW}' FROM runs WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "run not found")
        created_by, status, draft_sha, graph_version, registration, expired = row
        if status != "pending_approval":
            raise HTTPException(409, f"run is {status}, not pending approval")
        if expired:
            raise HTTPException(409, f"the approval window ({APPROVAL_WINDOW}) has passed")
        if created_by == principal.user_id:
            raise HTTPException(403, "you cannot decide on your own run")
        if draft_sha != body.draft_sha256:
            raise HTTPException(409, "the draft has changed since you reviewed it: reload it")
        if graph_version not in SERVED_GRAPH_VERSIONS:
            raise HTTPException(409, f"graph v{graph_version} is not served by this release: expire this run")
        edited = None
        if body.action == "edit":
            assert body.edited_draft is not None
            original = await latest_draft(conn, run_id)
            if original is None:
                raise HTTPException(409, "run has no draft")
            edited = checked_edit(body.edited_draft, original, registration)
        # The compare-and-set is the guarantee; the row lock above only makes the error messages precise.
        cur = await conn.execute(
            f"UPDATE runs SET status = 'decided', heartbeat_at = now(), updated_at = now() "  # noqa: S608
            f"WHERE run_id = %s AND status = 'pending_approval' AND created_by <> %s AND draft_sha256 = %s "
            f"AND updated_at >= now() - interval '{APPROVAL_WINDOW}' RETURNING run_id",
            (run_id, principal.user_id, body.draft_sha256),
        )
        if await cur.fetchone() is None:
            raise HTTPException(409, "run could not be decided")
        cur = await conn.execute(
            "INSERT INTO approvals (run_id, approver_id, action, draft_sha256, edited_draft, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING approval_id",
            (run_id, principal.user_id, body.action, body.draft_sha256, json.dumps(edited) if edited else None,
             body.reason),
        )  # fmt: skip
        approval_row = await cur.fetchone()
        assert approval_row is not None
        approval_id = str(approval_row[0])
        await audit.append(conn, principal.user_id, "decision", run_id,
                           {"approval_id": approval_id, "action": body.action, "draft_sha256": body.draft_sha256,
                            "reason": body.reason, "created_by": created_by})  # fmt: skip
        await conn.execute(
            "INSERT INTO run_events (run_id, seq, kind, payload) "
            "SELECT %s, coalesce(max(seq), 0) + 1, 'status', %s FROM run_events WHERE run_id = %s",
            (run_id, json.dumps({"status": "decided", "action": body.action, "approval_id": approval_id}), run_id),
        )
    await enqueue_run(st.jobs, run_id)  # if this fails, reap_runs re-enqueues the decided run
    return {"run_id": str(run_id), "status": "decided", "approval_id": approval_id}


@router.get("/approvals")
async def approval_queue(
    request: Request, principal: Annotated[Principal, Depends(dev_principal)]
) -> list[dict[str, Any]]:
    """Runs waiting for a decision, oldest first. Supervisors and admins only. Each item carries the draft
    and the draft_sha256 the decision must quote."""
    if not can_decide(principal):
        raise HTTPException(403, "only supervisors can see the approval queue")
    async with request.app.state.rw.connection() as conn:
        cur = await conn.execute(
            "SELECT r.run_id, r.registration, r.symptom, r.created_by, r.created_at, r.draft_sha256, r.graph_version, "
            "(SELECT payload->'draft' FROM run_events e WHERE e.run_id = r.run_id AND e.kind = 'draft' "
            " ORDER BY seq DESC LIMIT 1) FROM runs r WHERE r.status = 'pending_approval' ORDER BY r.created_at"
        )
        rows = await cur.fetchall()
    return [{"run_id": str(r[0]), "registration": r[1], "symptom": r[2], "created_by": r[3],
             "created_at": r[4].isoformat(), "draft_sha256": r[5], "graph_version": r[6], "draft": r[7],
             "own_run": r[3] == principal.user_id} for r in rows]  # fmt: skip
