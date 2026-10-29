"""The llm_calls cost ledger and the spend limits checked against it (DESIGN §2, §5.1)."""

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app.llm.errors import SpendLimitExceeded

DAY = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


@dataclass(frozen=True)
class CallContext:
    """Who a call is for. Every gateway call takes one, so every dollar is attributable."""

    kind: str  # e.g. "answer", "agent_step", "embed_query", "embed_corpus", "rerank", "judge"
    user_id: str | None = None
    run_id: UUID | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class CallRecord:
    ctx: CallContext
    user_hash: str | None
    model: str
    status: str
    latency_ms: int
    provider: str | None = None
    cost: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None


@dataclass(frozen=True)
class Spent:
    run: float
    user_day: float
    global_day: float


@dataclass(frozen=True)
class Caps:
    run: float
    user_day: float
    global_day: float


def user_hash(salt: str, user_id: str | None) -> str | None:
    """Salted, so ledger rows can be grouped per user without storing who the user is."""
    if user_id is None:
        return None
    return hmac.new(salt.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]


def check_caps(spent: Spent, estimate: float, caps: Caps, has_run: bool, has_user: bool) -> None:
    checks = [("global daily", spent.global_day, caps.global_day, True),
              ("user daily", spent.user_day, caps.user_day, has_user),
              ("run", spent.run, caps.run, has_run)]  # fmt: skip
    for scope, already, cap, applies in checks:
        if applies and already + estimate > cap:
            raise SpendLimitExceeded(scope, already, estimate, cap)


class Ledger(Protocol):
    async def spent(self, run_id: UUID | None, user_hash: str | None) -> Spent: ...
    async def record(self, rec: CallRecord, config_version: str) -> None: ...


class PostgresLedger:
    """Concurrent calls can each pass the check before either is recorded, so a cap can be overshot by
    the calls in flight (at most a few cents at the per-user concurrency limit). That's accepted."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def spent(self, run_id: UUID | None, user_hash: str | None) -> Spent:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT coalesce(sum(cost) FILTER (WHERE run_id = %(run)s), 0),
                       coalesce(sum(cost) FILTER (WHERE user_hash = %(user)s AND ts >= {DAY}), 0),
                       coalesce(sum(cost) FILTER (WHERE ts >= {DAY}), 0)
                FROM llm_calls
                WHERE ts >= {DAY} OR run_id = %(run)s
                """,  # noqa: S608  (DAY is a constant expression, values are bound parameters)
                {"run": run_id, "user": user_hash},
            )
            row = await cur.fetchone()
        assert row is not None
        run, user_day, global_day = (float(Decimal(v)) for v in row)
        return Spent(run=run, user_day=user_day, global_day=global_day)

    async def record(self, rec: CallRecord, config_version: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO llm_calls (run_id, user_hash, kind, model, provider, prompt_tokens, completion_tokens,
                                       cached_tokens, cost, latency_ms, status, prompt_version, config_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (rec.ctx.run_id, rec.user_hash, rec.ctx.kind, rec.model, rec.provider, rec.prompt_tokens,
                 rec.completion_tokens, rec.cached_tokens, rec.cost, rec.latency_ms, rec.status,
                 rec.ctx.prompt_version, config_version),
            )  # fmt: skip
