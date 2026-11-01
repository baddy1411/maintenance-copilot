"""Per-user rate limits in Postgres (DESIGN §2, ADR-0009). Spend caps ($1 per user per day, $5 per day globally)
are checked before every model call by the gateway (app/llm/ledger.py). Past them, Q&A degrades to retrieval
only and agent runs fail with the reason. These limits stop the request before any work starts.

Windows are UTC-aligned (hour, day). One atomic upsert per check. Users are stored as the same salted hash the
cost ledger uses, so `limits` doesn't hold user ids.
"""

from typing import Any, Literal

from fastapi import HTTPException
from psycopg import AsyncConnection

from app.config import Settings
from app.llm.ledger import user_hash

Window = Literal["hour", "day"]
LIVE_RUN_STATUSES = ["queued", "running", "decided"]  # pending_approval waits on a person, not on compute


def limit_subject(s: Settings, user_id: str) -> str:
    """How `limits` identifies a user: the same salted hash the cost ledger uses."""
    h = user_hash(s.user_hash_salt.get_secret_value(), user_id)
    assert h is not None
    return h


def too_many(detail: str, retry_after_s: int) -> HTTPException:
    return HTTPException(429, detail, headers={"Retry-After": str(max(1, retry_after_s))})


async def take(conn: AsyncConnection[Any], subject: str, kind: str, window: Window, limit: int) -> None:
    """Count one use in the current window. Raises 429 with Retry-After once `limit` is exceeded. Attempts over
    the limit are counted too, so hammering doesn't reset anything."""
    cur = await conn.execute(
        f"""
        WITH w AS (SELECT date_trunc('{window}', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS start)
        INSERT INTO limits (subject, kind, window_start, counter) SELECT %s, %s, w.start, 1 FROM w
        ON CONFLICT (subject, kind, window_start) DO UPDATE SET counter = limits.counter + 1
        RETURNING counter, ceil(extract(epoch FROM window_start + interval '1 {window}' - now()))::int
        """,  # noqa: S608 - `window` is a Literal, never input
        (subject, kind),
    )
    row = await cur.fetchone()
    assert row is not None
    count, retry_after = int(row[0]), int(row[1])
    if count > limit:
        raise too_many(f"limit of {limit} {kind} per {window} reached", retry_after)


async def check_concurrent_runs(conn: AsyncConnection[Any], created_by: str, limit: int) -> None:
    """At most `limit` runs per user using the worker at once. The advisory lock is held until the caller's
    transaction commits (the run insert), so two simultaneous requests can't both slip under the limit."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('runs:' || %s))", (created_by,))
    cur = await conn.execute(
        "SELECT count(*) FROM runs WHERE created_by = %s AND status = ANY(%s)", (created_by, LIVE_RUN_STATUSES)
    )
    row = await cur.fetchone()
    if row and int(row[0]) >= limit:
        raise too_many(f"you already have {row[0]} runs in progress (limit {limit})", 30)
