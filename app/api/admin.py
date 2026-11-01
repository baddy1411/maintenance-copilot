"""Audit log reads (DESIGN §7.3: supervisors and admins read the audit log).

GET /audit/runs/{run_id}   supervisors, admins   that run's entries, oldest first
GET /audit/verify          admins                walks the whole hash chain
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app import audit
from app.api.auth import Principal, admin, supervisor

router = APIRouter()


@router.get("/audit/runs/{run_id}")
async def run_audit(
    run_id: UUID, request: Request, _: Annotated[Principal, Depends(supervisor)]
) -> list[dict[str, Any]]:
    async with request.app.state.rw.connection() as conn:
        cur = await conn.execute(
            "SELECT id, ts, actor, action, payload_redacted, row_hash FROM audit_log WHERE run_id = %s ORDER BY id",
            (run_id,),
        )
        rows = await cur.fetchall()
    return [{"id": r[0], "ts": r[1].isoformat(), "actor": r[2], "action": r[3], "payload": r[4], "row_hash": r[5]}
            for r in rows]  # fmt: skip


@router.get("/audit/verify")
async def verify_chain(request: Request, _: Annotated[Principal, Depends(admin)]) -> dict[str, Any]:
    async with request.app.state.rw.connection() as conn:
        ok, first_bad, checked = await audit.verify(conn)
    return {"intact": ok, "first_bad_id": first_bad, "rows_checked": checked}
