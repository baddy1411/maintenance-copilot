"""Agent runs over HTTP (DESIGN §6.3, ADR-0004). The API never executes a run: it creates the row, defers the
job, and streams the run's events from Postgres. Any API replica can serve any run's stream.

    POST /runs                {registration, symptom}  + Idempotency-Key   -> 202 {run_id, status, events_url}
    GET  /runs/{run_id}                                                      -> status, versions, draft
    GET  /runs/{run_id}/events?after=N   (or Last-Event-ID)                  -> SSE, one event per run_event
"""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from app.agent.graph import GRAPH_VERSION, PROMPT_VERSION, STATE_SCHEMA_VERSION
from app.agent.schemas import REGISTRATION
from app.api.auth import Principal, dev_principal
from app.jobs import enqueue_run

router = APIRouter()
STREAM_POLL_S = 0.3
STREAM_MAX_S = 600.0  # after this the stream ends; the client re-attaches with Last-Event-ID
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
    """Only the creator for now; supervisors arrive with #13. A run that isn't yours is a 404, so its
    existence isn't revealed (security review S5)."""
    async with request.app.state.rw.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id, registration, symptom, status, graph_version, prompt_version, config_version, "
            "state_schema_version, draft_sha256, cost, attempts, created_at, updated_at "
            "FROM runs WHERE run_id = %s AND created_by = %s",
            (run_id, principal.user_id),
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
    while time.monotonic() < deadline:
        async with request.app.state.rw.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, kind, payload FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT 200",
                (run_id, seq),
            )
            rows = await cur.fetchall()
            cur = await conn.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
            status_row = await cur.fetchone()
        for s, kind, payload in rows:
            seq = s
            yield ServerSentEvent(id=str(s), event=kind, data=payload)
        if not rows and status_row and status_row[0] in END_STATES:
            yield ServerSentEvent(event="end", data={"status": status_row[0]})
            return
        if not rows:
            await asyncio.sleep(STREAM_POLL_S)
