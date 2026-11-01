"""Executes agent runs as worker jobs (ADR-0004, DESIGN §6.3), and reaps runs whose worker died.

A run's lifecycle in `runs.status`:
    queued -> running -> pending_approval            (this slice; #12 continues to decided -> written/rejected)
                      -> failed                      (known failure, or no progress after run_max_attempts)

The thread_id of the LangGraph checkpoint is the run_id. Each completed node is checkpointed before the next
starts (durability "sync"), so a run interrupted anywhere resumes from its last completed step. The step that
was in progress runs again, at most one LLM call's cost.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.agent.approval import ApprovalInvalid
from app.agent.graph import SERVED_GRAPH_VERSIONS, STATE_SCHEMA_VERSION, AgentDeps, AgentState, build_graph
from app.config import Settings
from app.llm import CreditsExhausted, ModelUnavailable, SpendLimitExceeded, StructuredOutputError, UnsupportedModel

logger = logging.getLogger(__name__)
CheckpointPool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]]

# A model or budget problem won't fix itself on retry: fail the run with the reason.
# ApprovalInvalid means a write was attempted that the approvals row doesn't authorise: nothing was written,
# and retrying can't change that.
KNOWN_FAILURES = (SpendLimitExceeded, CreditsExhausted, ModelUnavailable, StructuredOutputError, UnsupportedModel,
                  ApprovalInvalid)  # fmt: skip


@dataclass
class RunDeps:
    settings: Settings
    rw_pool: AsyncConnectionPool  # app_rw: runs, run_events, ledger
    cp_pool: "CheckpointPool"  # app_rw with autocommit + dict_row + prepare_threshold=0, for the checkpointer
    agent: AgentDeps


async def emit(pool: AsyncConnectionPool, run_id: UUID, kind: str, payload: dict[str, Any]) -> None:
    """Append one event and refresh the heartbeat. One job runs a run at a time (procrastinate lock), so
    max(seq) + 1 can't race."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO run_events (run_id, seq, kind, payload) "
            "SELECT %s, coalesce(max(seq), 0) + 1, %s, %s FROM run_events WHERE run_id = %s",
            (run_id, kind, json.dumps(payload, default=str), run_id),
        )
        await conn.execute("UPDATE runs SET heartbeat_at = now(), updated_at = now() WHERE run_id = %s", (run_id,))


async def set_status(pool: AsyncConnectionPool, run_id: UUID, status: str, **fields: Any) -> None:
    sets = ", ".join(["status = %s", "updated_at = now()", *(f"{k} = %s" for k in fields)])
    async with pool.connection() as conn:
        await conn.execute(
            f"UPDATE runs SET {sets}, cost = (SELECT coalesce(sum(cost), 0) FROM llm_calls WHERE run_id = %s) "  # noqa: S608
            "WHERE run_id = %s",
            (status, *fields.values(), run_id, run_id),
        )


async def fail(deps: RunDeps, run_id: UUID, reason: str) -> str:
    await emit(deps.rw_pool, run_id, "status", {"status": "failed", "reason": reason})
    await set_status(deps.rw_pool, run_id, "failed")
    return "failed"


async def execute_run(run_id: UUID, deps: RunDeps) -> str:
    """Run (or resume) one agent run until it pauses for approval, is written or rejected, or fails. Returns the
    resulting status. Safe to call twice: a run that isn't queued, running or decided is left alone."""
    async with deps.rw_pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE runs SET status = 'running', attempts = attempts + 1, heartbeat_at = now(), updated_at = now() "
            "WHERE run_id = %s AND status IN ('queued', 'running', 'decided') "
            "RETURNING registration, symptom, attempts, graph_version, state_schema_version",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return "skipped"
    registration, symptom, attempts, graph_version, schema_version = row
    if attempts > deps.settings.run_max_attempts:
        return await fail(deps, run_id, f"no progress after {deps.settings.run_max_attempts} attempts")
    if graph_version not in SERVED_GRAPH_VERSIONS or schema_version != STATE_SCHEMA_VERSION:
        # DESIGN §6.3.1: a release that drops a graph version must migrate or expire its runs before deploying.
        return await fail(deps, run_id, f"graph v{graph_version}/state v{schema_version} is not served by this release")

    graph = build_graph(deps.agent, AsyncPostgresSaver(deps.cp_pool))
    config: Any = {"configurable": {"thread_id": str(run_id)}}
    snap = await graph.aget_state(config)
    resuming = bool(snap.values)
    await emit(deps.rw_pool, run_id, "status", {"status": "running", "attempt": attempts, "resumed": resuming})
    start: AgentState | Command[Any] | None = None
    if resuming and snap.interrupts:
        # Paused at approval. Resume only if a decision has been recorded, and resume with what the
        # approvals row says, never with anything from a request. The write/reject node re-checks it anyway.
        decision = await load_decision(deps.rw_pool, run_id)
        if decision is None:  # crashed after the checkpoint but before the status update
            return await pause(deps, run_id, snap.interrupts[0].value)
        start = Command(resume=decision)
    elif not resuming:
        start = {"run_id": str(run_id), "registration": registration, "symptom": symptom, "messages": [],
                 "evidence": {}, "steps": 0, "tool_errors": {}}  # fmt: skip
    try:
        async for update in graph.astream(start, config, stream_mode="updates", durability="sync"):
            for node, out in update.items():
                if node == "__interrupt__":
                    return await pause(deps, run_id, out[0].value)
                event = (out or {}).get("event")
                if event:
                    await emit(deps.rw_pool, run_id, event["kind"], {"node": node, **event})
    except KNOWN_FAILURES as exc:
        logger.warning("run %s failed: %s", run_id, exc)
        return await fail(deps, run_id, f"{type(exc).__name__}: {exc}")
    status = await current_status(deps.rw_pool, run_id)
    if status in ("written", "rejected"):  # committed by the write/reject node, with its audit entry
        await emit(deps.rw_pool, run_id, "status", {"status": status})
        return status
    return await fail(deps, run_id, "graph ended without a decision being recorded")


async def load_decision(pool: AsyncConnectionPool, run_id: UUID) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT approval_id, action FROM approvals WHERE run_id = %s", (run_id,))
        row = await cur.fetchone()
    return {"approval_id": str(row[0]), "action": row[1]} if row else None


async def current_status(pool: AsyncConnectionPool, run_id: UUID) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
        row = await cur.fetchone()
    return str(row[0]) if row else "missing"


async def pause(deps: RunDeps, run_id: UUID, payload: dict[str, Any]) -> str:
    await set_status(deps.rw_pool, run_id, "pending_approval", draft_sha256=payload["draft_sha256"])
    await emit(deps.rw_pool, run_id, "status", {"status": "pending_approval", "draft_sha256": payload["draft_sha256"]})
    return "pending_approval"


async def stale_runs(pool: AsyncConnectionPool, stale_after_s: int) -> list[tuple[UUID, int]]:
    """Runs that should be making progress but aren't: queued, running or decided, with no heartbeat for too
    long. That covers a killed worker, and a queued or decided run whose job was lost."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id, attempts FROM runs WHERE status IN ('queued', 'running', 'decided') "
            "AND coalesce(heartbeat_at, created_at) < now() - make_interval(secs => %s) ORDER BY created_at",
            (stale_after_s,),
        )
        return [(UUID(str(r[0])), int(r[1])) for r in await cur.fetchall()]


async def expire_runs(pool: AsyncConnectionPool, where: str, params: tuple[Any, ...], reason: str) -> list[UUID]:
    """Expire matching runs, one transaction each: status, audit entry, event. `where` is a fixed SQL
    fragment from this module, never user input."""
    from app import audit

    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT run_id FROM runs WHERE {where}", params)  # noqa: S608
        candidates = [UUID(str(r[0])) for r in await cur.fetchall()]
    expired = []
    for run_id in candidates:
        async with pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                f"UPDATE runs SET status = 'expired', updated_at = now() WHERE run_id = %s AND {where} "  # noqa: S608
                "RETURNING created_by, graph_version",
                (run_id, *params),
            )
            row = await cur.fetchone()
            if row is None:  # decided or changed in the meantime
                continue
            await audit.append(conn, "system", "run_expired", run_id,
                               {"reason": reason, "created_by": row[0], "graph_version": row[1]})  # fmt: skip
            await conn.execute(
                "INSERT INTO run_events (run_id, seq, kind, payload) "
                "SELECT %s, coalesce(max(seq), 0) + 1, 'status', %s FROM run_events WHERE run_id = %s",
                (run_id, json.dumps({"status": "expired", "reason": reason}), run_id),
            )
        expired.append(run_id)
    return expired


async def expire_approvals(pool: AsyncConnectionPool, window_days: int = 7) -> list[UUID]:
    """Runs waiting for a decision longer than the window (DESIGN §6.3)."""
    return await expire_runs(pool, "status = 'pending_approval' AND updated_at < now() - make_interval(days => %s)",
                             (window_days,), f"no decision within {window_days} days")  # fmt: skip


LIVE_STATUSES = ("queued", "running", "pending_approval", "decided")


async def unserved_runs(pool: AsyncConnectionPool) -> list[tuple[UUID, str, str]]:
    """The pre-deploy check (DESIGN §6.3.1): live runs on a graph version this release can't resume."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id, status, graph_version FROM runs WHERE status = ANY(%s) AND NOT graph_version = ANY(%s)",
            (list(LIVE_STATUSES), sorted(SERVED_GRAPH_VERSIONS)),
        )
        return [(UUID(str(r[0])), r[1], r[2]) for r in await cur.fetchall()]


async def expire_unserved(pool: AsyncConnectionPool) -> list[UUID]:
    return await expire_runs(pool, "status = ANY(%s) AND NOT graph_version = ANY(%s)",
                             (list(LIVE_STATUSES), sorted(SERVED_GRAPH_VERSIONS)),
                             "graph version not served by this release")  # fmt: skip
