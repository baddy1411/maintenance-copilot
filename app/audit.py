"""Append-only, hash-chained audit log (DESIGN §5.1, security review S8).

Each row stores `prev_hash` (the previous row's `row_hash`) and its own `row_hash`, which is the sha256 of
the previous hash plus the canonical JSON of (ts, actor, action, run_id, payload). Editing or deleting any
row breaks every hash after it, and `verify()` finds the first break. Service roles can only INSERT and
SELECT on `audit_log` (migration 0001), so a running service can't rewrite history at all. The chain is
for detecting what someone with owner rights might do.

`append()` must run inside the caller's transaction, so the audit row commits or rolls back with the change
it records. A transaction-level advisory lock serialises appends, so two concurrent writers can't both link
to the same previous row and fork the chain.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

CHAIN_LOCK = 0x41554449  # "AUDI": the advisory-lock key for audit appends
GENESIS = "0" * 64


def row_hash(prev: str, ts: datetime, actor: str, action: str, run_id: UUID | None, payload: dict[str, Any]) -> str:
    # UTC always: Postgres returns timestamptz in the session's time zone, so the same instant can read back
    # as "+01:00" and would no longer hash the same.
    body = json.dumps({"ts": ts.astimezone(UTC).isoformat(), "actor": actor, "action": action,
                       "run_id": str(run_id) if run_id else None, "payload": payload},
                      sort_keys=True, separators=(",", ":"), default=str)  # fmt: skip
    return hashlib.sha256((prev + body).encode()).hexdigest()


async def append(conn: AsyncConnection[Any], actor: str, action: str, run_id: UUID | None,
                 payload: dict[str, Any]) -> str:  # fmt: skip
    """Append one row inside the caller's open transaction. Returns its hash."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (CHAIN_LOCK,))
    cur = await conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
    last = await cur.fetchone()
    prev = str(last[0]) if last else GENESIS
    ts = datetime.now(UTC)
    h = row_hash(prev, ts, actor, action, run_id, payload)
    await conn.execute(
        "INSERT INTO audit_log (ts, actor, action, run_id, payload_redacted, prev_hash, row_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ts, actor, action, run_id, json.dumps(payload, default=str), prev, h),
    )
    return h


async def verify(conn: AsyncConnection[Any]) -> tuple[bool, int | None, int]:
    """Walk the chain in id order. Returns (intact, id of the first bad row, rows checked)."""
    prev, n = GENESIS, 0
    cur = await conn.execute(
        "SELECT id, ts, actor, action, run_id, payload_redacted, prev_hash, row_hash FROM audit_log ORDER BY id"
    )
    async for rid, ts, actor, action, run_id, payload, prev_hash, h in cur:
        n += 1
        if prev_hash != prev or row_hash(prev, ts, actor, action, run_id, payload) != h:
            return False, int(rid), n
        prev = h
    return True, None, n
