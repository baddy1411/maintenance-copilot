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
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import GRAPH_VERSION, STATE_SCHEMA_VERSION, AgentDeps, AgentState, build_graph
from app.config import Settings
from app.llm import CreditsExhausted, ModelUnavailable, SpendLimitExceeded, StructuredOutputError, UnsupportedModel

logger = logging.getLogger(__name__)
CheckpointPool = AsyncConnectionPool[AsyncConnection[dict[str, Any]]]

# A model or budget problem won't fix itself on retry: fail the run with the reason.
KNOWN_FAILURES = (SpendLimitExceeded, CreditsExhausted, ModelUnavailable, StructuredOutputError, UnsupportedModel)


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
    """Run (or resume) one agent run until it pauses for approval or fails. Returns the resulting status.
    Safe to call twice: a run that isn't queued or running is left alone."""
    async with deps.rw_pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE runs SET status = 'running', attempts = attempts + 1, heartbeat_at = now(), updated_at = now() "
            "WHERE run_id = %s AND status IN ('queued', 'running') "
            "RETURNING registration, symptom, attempts, graph_version, state_schema_version",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return "skipped"
    registration, symptom, attempts, graph_version, schema_version = row
    if attempts > deps.settings.run_max_attempts:
        return await fail(deps, run_id, f"no progress after {deps.settings.run_max_attempts} attempts")
    if graph_version != GRAPH_VERSION or schema_version != STATE_SCHEMA_VERSION:
        # DESIGN §6.3.1: a release that drops a graph version must migrate or expire its runs before deploying.
        return await fail(deps, run_id, f"graph v{graph_version}/state v{schema_version} is not served by this release")

    graph = build_graph(deps.agent, AsyncPostgresSaver(deps.cp_pool))
    config: Any = {"configurable": {"thread_id": str(run_id)}}
    snap = await graph.aget_state(config)
    resuming = bool(snap.values)
    await emit(deps.rw_pool, run_id, "status", {"status": "running", "attempt": attempts, "resumed": resuming})
    if resuming and snap.interrupts:  # crashed after the checkpoint but before the status update
        return await pause(deps, run_id, snap.interrupts[0].value)

    start: AgentState | None = None
    if not resuming:
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
    return await fail(deps, run_id, "graph ended without a draft")


async def pause(deps: RunDeps, run_id: UUID, payload: dict[str, Any]) -> str:
    await set_status(deps.rw_pool, run_id, "pending_approval", draft_sha256=payload["draft_sha256"])
    await emit(deps.rw_pool, run_id, "status", {"status": "pending_approval", "draft_sha256": payload["draft_sha256"]})
    return "pending_approval"


async def stale_runs(pool: AsyncConnectionPool, stale_after_s: int) -> list[tuple[UUID, int]]:
    """Runs that should be making progress but aren't: queued or running, with no heartbeat for too long.
    That covers a killed worker, and a queued run whose job was lost."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id, attempts FROM runs WHERE status IN ('queued', 'running') "
            "AND coalesce(heartbeat_at, created_at) < now() - make_interval(secs => %s) ORDER BY created_at",
            (stale_after_s,),
        )
        return [(UUID(str(r[0])), int(r[1])) for r in await cur.fetchall()]
