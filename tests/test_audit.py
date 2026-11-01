"""The hash-chained audit log (app/audit.py): intact chains verify; edits, deletions and forks don't."""

import asyncio
from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app import audit
from app.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool(db: Settings) -> AsyncIterator[AsyncConnectionPool]:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE audit_log")
    async with AsyncConnectionPool(db.dsn("app_rw"), min_size=1, max_size=12, open=False) as p:
        yield p


async def add(pool: AsyncConnectionPool, n: int = 3) -> None:
    for i in range(n):
        async with pool.connection() as conn, conn.transaction():
            await audit.append(conn, f"user-{i}", "decision", None, {"i": i, "note": "café ✓"})


async def check(pool: AsyncConnectionPool) -> tuple[bool, int | None, int]:
    async with pool.connection() as conn:
        return await audit.verify(conn)


async def test_intact_chain_verifies(pool: AsyncConnectionPool) -> None:
    await add(pool, 5)
    assert await check(pool) == (True, None, 5)


async def test_editing_a_row_breaks_the_chain_there(pool: AsyncConnectionPool, db: Settings) -> None:
    await add(pool, 5)
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:  # only the owner can do this
        ids = [r[0] for r in conn.execute("SELECT id FROM audit_log ORDER BY id")]
        conn.execute("UPDATE audit_log SET actor = 'someone-else' WHERE id = %s", (ids[2],))
    assert await check(pool) == (False, ids[2], 3)


async def test_deleting_a_row_breaks_the_chain_after_it(pool: AsyncConnectionPool, db: Settings) -> None:
    await add(pool, 5)
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM audit_log ORDER BY id")]
        conn.execute("DELETE FROM audit_log WHERE id = %s", (ids[1],))
    ok, bad, _ = await check(pool)
    assert not ok and bad == ids[2]


async def test_concurrent_appends_do_not_fork_the_chain(pool: AsyncConnectionPool) -> None:
    async def one(i: int) -> None:
        async with pool.connection() as conn, conn.transaction():
            await audit.append(conn, f"u{i}", "decision", None, {"i": i})

    await asyncio.gather(*(one(i) for i in range(10)))
    assert await check(pool) == (True, None, 10)


async def test_an_audit_row_rolls_back_with_its_transaction(pool: AsyncConnectionPool) -> None:
    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        async with pool.connection() as conn, conn.transaction():
            await audit.append(conn, "u", "decision", None, {})
            raise Abort
    assert await check(pool) == (True, None, 0)


async def test_chain_verifies_from_a_session_in_another_time_zone(pool: AsyncConnectionPool) -> None:
    """timestamptz comes back in the session's zone; hashing must not depend on it."""
    await add(pool, 3)
    async with pool.connection() as conn:
        await conn.execute("SET TIME ZONE 'Asia/Kolkata'")
        assert await audit.verify(conn) == (True, None, 3)
        await conn.execute("RESET TIME ZONE")
