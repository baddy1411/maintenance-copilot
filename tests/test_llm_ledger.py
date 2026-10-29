"""PostgresLedger on the migrated schema, as the app_rw role. Needs Docker."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.llm.errors import SpendLimitExceeded
from app.llm.ledger import CallContext, CallRecord, Caps, PostgresLedger, check_caps

pytestmark = pytest.mark.integration


@pytest.fixture
async def ledger(db: Settings) -> AsyncIterator[PostgresLedger]:
    async with AsyncConnectionPool(db.dsn("app_rw"), min_size=1, max_size=2, open=False) as pool:
        yield PostgresLedger(pool)


def rec(cost: float, run_id: object = None, user: str | None = None, kind: str = "answer") -> CallRecord:
    return CallRecord(ctx=CallContext(kind=kind, run_id=run_id), user_hash=user, model="m", status="ok",  # type: ignore[arg-type]
                      latency_ms=5, cost=cost)  # fmt: skip


async def test_spent_sums_per_run_per_user_today_and_globally(ledger: PostgresLedger) -> None:
    before = await ledger.spent(None, None)
    run, other_run = uuid4(), uuid4()
    for r in (rec(0.01, run, "u1"), rec(0.02, run, "u1"), rec(0.05, other_run, "u2"), rec(0.10, None, None)):
        await ledger.record(r, config_version="cfg1")
    s = await ledger.spent(run, "u1")
    assert s.run == pytest.approx(0.03)
    assert s.user_day == pytest.approx(0.03)
    assert s.global_day == pytest.approx(before.global_day + 0.18)


async def test_record_stores_versions_for_attribution(ledger: PostgresLedger, db: Settings) -> None:
    run = uuid4()
    r = CallRecord(ctx=CallContext(kind="agent_step", run_id=run, prompt_version="p-abc"), user_hash="u9",
                   model="anthropic/claude-haiku-5.5", provider="Anthropic", status="ok", latency_ms=812,
                   cost=0.0004, prompt_tokens=120, completion_tokens=30, cached_tokens=0)  # fmt: skip
    await ledger.record(r, config_version="cfg-123")
    async with ledger.pool.connection() as conn:
        cur = await conn.execute("SELECT kind, provider, prompt_version, config_version, cost::float FROM llm_calls "
                                 "WHERE run_id = %s", (run,))  # fmt: skip
        assert await cur.fetchall() == [("agent_step", "Anthropic", "p-abc", "cfg-123", 0.0004)]


def test_check_caps_skips_scopes_that_do_not_apply() -> None:
    from app.llm.ledger import Spent

    caps = Caps(run=0.25, user_day=1.0, global_day=5.0)
    check_caps(Spent(run=9.0, user_day=9.0, global_day=0.0), 0.01, caps, has_run=False, has_user=False)
    with pytest.raises(SpendLimitExceeded, match="user daily"):
        check_caps(Spent(run=0.0, user_day=0.995, global_day=0.0), 0.01, caps, has_run=False, has_user=True)
