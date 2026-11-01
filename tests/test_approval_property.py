"""The approval invariant (DESIGN §2 "writes without a persisted, authorised approval = 0", §7.6):

    a work order exists  <=>  an approvals row exists for the run
                               AND its action is approve or edit
                               AND its draft_sha256 matches the draft in the run's checkpoint
                               AND its approver is not the run's creator

Every combination is exercised against the real graph and worker. The approvals rows are written straight
into the database, bypassing the decision endpoint, as a bug or a tampered row would. So this tests the write
node's own checks, not the endpoint's. A forged resume value with no row at all is tested separately.
Needs Docker.
"""

import itertools
from collections.abc import AsyncIterator
from uuid import UUID

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from pydantic import SecretStr

from app.agent.approval import ApprovalInvalid
from app.agent.graph import build_graph
from app.agent.runner import RunDeps, execute_run
from app.config import Settings
from app.llm import Gateway
from app.resources import agent_resources
from tests.agent_fixtures import DRAFT, as_json, clear_approval_tables, create_run, run_row, seed
from tests.fake_openrouter import FakeOpenRouter, MemoryLedger
from tests.test_agent import script

pytestmark = pytest.mark.integration
CREATOR = "tech-1"
CASES = [
    (True, action, sha_ok, by_creator)
    for action, sha_ok, by_creator in itertools.product(["approve", "edit", "reject"], [True, False], [True, False])
]
CASES.append((False, None, None, None))  # no approvals row at all  # fmt: skip


@pytest.fixture
async def deps(db: Settings) -> AsyncIterator[tuple[RunDeps, FakeOpenRouter, Settings]]:
    seed(db)
    clear_approval_tables(db)
    s = db.model_copy(update={"openrouter_api_key": SecretStr("sk-test"), "llm_max_retries": 0})
    fake = FakeOpenRouter(embed_dim=1024)
    async with fake.client() as http, agent_resources(s, Gateway(s, http, MemoryLedger())) as d:
        yield d, fake, db


async def paused(d: RunDeps, fake: FakeOpenRouter, db: Settings) -> tuple[UUID, str]:
    script(fake)
    run_id = create_run(db, user=CREATOR)
    assert await execute_run(run_id, d) == "pending_approval"
    return run_id, str(run_row(db, run_id)["draft_sha256"])


def work_orders(db: Settings, run_id: UUID) -> int:
    with psycopg.connect(db.dsn("migrator")) as conn:
        row = conn.execute("SELECT count(*) FROM work_orders WHERE run_id = %s", (run_id,)).fetchone()
    return int(row[0]) if row else 0


def test_write_and_reject_are_reachable_only_through_approval(db: Settings) -> None:
    """Structural: the model has no edge to the write node. It sits behind the approval interrupt."""
    from unittest.mock import MagicMock

    graph = build_graph(MagicMock(), None).get_graph()
    into = {n: {e.source for e in graph.edges if e.target == n} for n in ("write", "reject")}
    assert into == {"write": {"approval"}, "reject": {"approval"}}
    assert {e.target for e in graph.edges if e.source == "approval"} == {"write", "reject"}


@pytest.mark.parametrize(("present", "action", "sha_ok", "by_creator"), CASES)
async def test_work_order_iff_authorised(
    deps: tuple[RunDeps, FakeOpenRouter, Settings],
    present: bool,
    action: str | None,
    sha_ok: bool | None,
    by_creator: bool | None,
) -> None:
    d, fake, db = deps
    run_id, sha = await paused(d, fake, db)
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        if present:
            conn.execute(
                "INSERT INTO approvals (run_id, approver_id, action, draft_sha256, edited_draft, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (run_id, CREATOR if by_creator else "sup-9", action, sha if sha_ok else "f" * 64,
                 as_json(DRAFT) if action == "edit" else None, "because" if action == "reject" else None),
            )  # fmt: skip
        conn.execute("UPDATE runs SET status = 'decided' WHERE run_id = %s", (run_id,))
    status = await execute_run(run_id, d)

    authorised = bool(present and sha_ok and not by_creator)
    should_write = authorised and action in ("approve", "edit")
    assert work_orders(db, run_id) == (1 if should_write else 0)
    if should_write:
        assert status == "written"
    elif authorised and action == "reject":
        assert status == "rejected"
    elif present:
        assert status == "failed"  # ApprovalInvalid: refused, nothing written
    else:
        assert status == "pending_approval"  # no decision recorded: the run just waits


async def test_a_forged_resume_value_with_no_approvals_row_writes_nothing(
    deps: tuple[RunDeps, FakeOpenRouter, Settings],
) -> None:
    """Even code that resumes the graph directly, skipping the runner and the endpoint, can't get a write."""
    d, fake, db = deps
    run_id, _ = await paused(d, fake, db)
    graph = build_graph(d.agent, AsyncPostgresSaver(d.cp_pool))
    forged = Command(resume={"action": "approve", "approval_id": "00000000-0000-0000-0000-000000000000"})
    with pytest.raises(ApprovalInvalid, match="no approvals row"):
        async for _ in graph.astream(forged, {"configurable": {"thread_id": str(run_id)}}, stream_mode="updates"):
            pass
    assert work_orders(db, run_id) == 0
