"""What happens after a supervisor decides (DESIGN §6.3, security review S1/S2/S8).

The graph's write and reject nodes call these. They never trust the value the graph was resumed with: the
decision is re-read from the `approvals` row (written by the decision endpoint, with the approver from the
authenticated principal), and checked against the run and the draft in the checkpoint, inside the same
transaction as the work-order insert and the audit append. Commit all or nothing.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app import audit


class ApprovalInvalid(RuntimeError):
    """The approvals row doesn't authorise this write. Nothing was written."""


@dataclass(frozen=True)
class Approval:
    approval_id: UUID
    approver_id: str
    action: str
    draft_sha256: str
    edited_draft: dict[str, Any] | None
    reason: str | None
    created_by: str
    registration: str


async def _load(conn: Any, run_id: UUID) -> Approval:
    cur = await conn.execute(
        "SELECT a.approval_id, a.approver_id, a.action, a.draft_sha256, a.edited_draft, a.reason, r.created_by, "
        "r.registration FROM approvals a JOIN runs r USING (run_id) WHERE a.run_id = %s FOR UPDATE OF r",
        (run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise ApprovalInvalid("no approvals row for this run")
    return Approval(UUID(str(row[0])), *row[1:])


def _check(a: Approval, draft_sha256: str | None, actions: set[str]) -> None:
    if a.action not in actions:
        raise ApprovalInvalid(f"approval action {a.action!r} does not allow this")
    if a.draft_sha256 != draft_sha256:
        raise ApprovalInvalid("approval was for a different draft (draft_sha256 mismatch)")
    if a.approver_id == a.created_by:
        raise ApprovalInvalid("the creator of a run cannot approve it")


async def write_work_order(pool: AsyncConnectionPool, run_id: UUID, state: dict[str, Any]) -> tuple[UUID, bool]:
    """Returns (work order id, newly written). Running it again after a commit (a worker that died before its
    checkpoint) returns the same work order and doesn't append a second audit row."""
    async with pool.connection() as conn, conn.transaction():
        a = await _load(conn, run_id)
        _check(a, state.get("draft_sha256"), {"approve", "edit"})
        draft = a.edited_draft if a.action == "edit" and a.edited_draft else state["draft"]
        if draft["registration"] != a.registration:  # the decision endpoint enforces this too
            raise ApprovalInvalid("work order registration differs from the run's aircraft")
        cur = await conn.execute(
            "INSERT INTO work_orders (run_id, approval_id, registration, title, priority, diagnosis, steps, citations, "
            "created_by, approved_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id) DO NOTHING RETURNING wo_id",
            (run_id, a.approval_id, draft["registration"], draft["title"], draft["priority"],
             json.dumps(state.get("diagnosis") or {}), json.dumps(draft["steps"]), json.dumps(draft["citations"]),
             a.created_by, a.approver_id),
        )  # fmt: skip
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute("SELECT wo_id FROM work_orders WHERE run_id = %s", (run_id,))
            existing = await cur.fetchone()
            assert existing is not None
            return UUID(str(existing[0])), False
        wo_id = UUID(str(row[0]))
        await audit.append(conn, a.approver_id, "work_order_written", run_id,
                           {"wo_id": str(wo_id), "approval_id": str(a.approval_id), "action": a.action,
                            "draft_sha256": a.draft_sha256, "title": draft["title"], "priority": draft["priority"],
                            "registration": draft["registration"], "created_by": a.created_by})  # fmt: skip
        await conn.execute("UPDATE runs SET status = 'written', updated_at = now() WHERE run_id = %s", (run_id,))
    return wo_id, True


async def reject_run(pool: AsyncConnectionPool, run_id: UUID, state: dict[str, Any]) -> bool:
    """Returns True if this call recorded the rejection (False on a re-run after a commit)."""
    async with pool.connection() as conn, conn.transaction():
        a = await _load(conn, run_id)
        _check(a, state.get("draft_sha256"), {"reject"})
        cur = await conn.execute(
            "UPDATE runs SET status = 'rejected', updated_at = now() WHERE run_id = %s AND status <> 'rejected' "
            "RETURNING run_id",
            (run_id,),
        )
        if await cur.fetchone() is None:
            return False
        await audit.append(conn, a.approver_id, "work_order_rejected", run_id,
                           {"approval_id": str(a.approval_id), "reason": a.reason, "draft_sha256": a.draft_sha256,
                            "created_by": a.created_by})  # fmt: skip
    return True
