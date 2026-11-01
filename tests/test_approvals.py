"""Supervisor decisions end to end (DESIGN §6.3): the decision endpoint's rules, then the worker resuming the
run into the write or reject node. Real FastAPI app, real Postgres, real graph; only OpenRouter is faked.
Needs Docker."""

import asyncio
import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import psycopg
import pytest
from pydantic import SecretStr

from app import audit
from app.agent.approval import write_work_order
from app.agent.graph import draft_sha256
from app.agent.runner import RunDeps, execute_run, expire_approvals
from app.api.main import create_app
from app.config import Settings
from app.llm import Gateway
from app.resources import agent_resources
from tests.agent_fixtures import clear_approval_tables, create_run, events, run_row, seed
from tests.fake_openrouter import FakeOpenRouter, MemoryLedger
from tests.test_agent import script

pytestmark = pytest.mark.integration
SUP = {"X-Dev-User": "sup-1", "X-Dev-Role": "supervisor"}


@dataclass
class Env:
    db: Settings
    http: httpx2.AsyncClient
    fake: FakeOpenRouter
    deps: RunDeps

    async def paused_run(self, creator: str = "tech-1", graph_version: str | None = None) -> tuple[UUID, str]:
        """A real run, driven through the graph to the approval interrupt."""
        script(self.fake)
        run_id = create_run(self.db, user=creator)
        if graph_version:
            with psycopg.connect(self.db.dsn("migrator"), autocommit=True) as conn:
                conn.execute("UPDATE runs SET graph_version = %s WHERE run_id = %s", (graph_version, run_id))
        assert await execute_run(run_id, self.deps) == "pending_approval"
        return run_id, str(run_row(self.db, run_id)["draft_sha256"])

    async def decide(self, run_id: UUID, body: dict[str, Any], headers: dict[str, str] = SUP) -> httpx2.Response:
        return await self.http.post(f"/runs/{run_id}/decision", json=body, headers=headers)

    def q(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.dsn("migrator")) as conn:
            return conn.execute(sql, params).fetchall()


@pytest.fixture
async def env(db: Settings) -> AsyncIterator[Env]:
    seed(db)
    clear_approval_tables(db)
    s = db.model_copy(update={"openrouter_api_key": SecretStr("sk-test"), "user_hash_salt": SecretStr("s"),
                              "llm_max_retries": 0})  # fmt: skip
    fake = FakeOpenRouter(embed_dim=1024)
    async with fake.client() as llm_http:
        gw = Gateway(s, llm_http, MemoryLedger())
        app = create_app(s, gw)
        async with app.router.lifespan_context(app), agent_resources(s, gw) as deps, httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as http:  # fmt: skip
            yield Env(s, http, fake, deps)


# ---- who may decide --------------------------------------------------------------------------------------


async def test_technicians_cannot_decide_or_see_the_queue(env: Env) -> None:
    run_id, sha = await env.paused_run()
    tech = {"X-Dev-User": "tech-2", "X-Dev-Role": "technician"}
    assert (await env.decide(run_id, {"action": "approve", "draft_sha256": sha}, tech)).status_code == 403
    assert (await env.http.get("/approvals", headers=tech)).status_code == 403


async def test_the_queue_shows_the_draft_and_the_hash_to_quote(env: Env) -> None:
    run_id, sha = await env.paused_run()
    queue = (await env.http.get("/approvals", headers=SUP)).json()
    assert [(q["run_id"], q["draft_sha256"], q["own_run"]) for q in queue] == [(str(run_id), sha, False)]
    assert queue[0]["draft"]["registration"] == "MX-001"


async def test_a_supervisor_cannot_approve_their_own_run(env: Env) -> None:
    run_id, sha = await env.paused_run(creator="sup-1")
    r = await env.decide(run_id, {"action": "approve", "draft_sha256": sha})
    assert r.status_code == 403 and env.q("SELECT * FROM approvals") == []


async def test_the_approver_comes_from_the_principal_not_the_body(env: Env) -> None:
    run_id, sha = await env.paused_run()
    r = await env.decide(run_id, {"action": "approve", "draft_sha256": sha, "approver_id": "ceo"})
    assert r.status_code == 202
    assert env.q("SELECT approver_id FROM approvals") == [("sup-1",)]


# ---- what may be decided ---------------------------------------------------------------------------------


async def test_a_stale_draft_hash_is_refused(env: Env) -> None:
    run_id, _ = await env.paused_run()
    r = await env.decide(run_id, {"action": "approve", "draft_sha256": "0" * 64})
    assert r.status_code == 409 and "changed since you reviewed" in r.json()["detail"]
    assert run_row(env.db, run_id)["status"] == "pending_approval" and env.q("SELECT * FROM approvals") == []


async def test_only_one_decision_per_run(env: Env) -> None:
    run_id, sha = await env.paused_run()
    assert (await env.decide(run_id, {"action": "approve", "draft_sha256": sha})).status_code == 202
    r = await env.decide(run_id, {"action": "reject", "draft_sha256": sha, "reason": "late"},
                         {"X-Dev-User": "sup-2", "X-Dev-Role": "supervisor"})  # fmt: skip
    assert r.status_code == 409 and "is decided" in r.json()["detail"]


async def test_concurrent_approve_and_reject_one_wins(env: Env) -> None:
    run_id, sha = await env.paused_run()
    a, b = await asyncio.gather(
        env.decide(run_id, {"action": "approve", "draft_sha256": sha}),
        env.decide(run_id, {"action": "reject", "draft_sha256": sha, "reason": "no"},
                   {"X-Dev-User": "sup-2", "X-Dev-Role": "supervisor"}),
    )  # fmt: skip
    assert sorted([a.status_code, b.status_code]) == [202, 409]
    assert len(env.q("SELECT * FROM approvals")) == 1
    assert len(env.q("SELECT * FROM audit_log WHERE action = 'decision'")) == 1
    assert len(env.q("SELECT * FROM procrastinate_jobs WHERE task_name = 'run_agent'")) == 1


async def test_reject_needs_a_reason_and_edit_needs_a_draft(env: Env) -> None:
    run_id, sha = await env.paused_run()
    assert (await env.decide(run_id, {"action": "reject", "draft_sha256": sha})).status_code == 422
    assert (await env.decide(run_id, {"action": "edit", "draft_sha256": sha})).status_code == 422


async def test_an_edit_cannot_change_the_aircraft_or_add_citations(env: Env) -> None:
    run_id, sha = await env.paused_run()
    draft = (await env.http.get(f"/runs/{run_id}", headers=SUP)).json()["draft"]
    edit = {k: draft[k] for k in ("registration", "title", "priority", "diagnosis_summary", "steps")}
    edit["citations"] = [{"chunk_id": c["chunk_id"], "quote": c["quote"]} for c in draft["citations"]]
    other = {**edit, "registration": "MX-002"}
    r = await env.decide(run_id, {"action": "edit", "draft_sha256": sha, "edited_draft": other})
    assert r.status_code == 422 and "registration" in r.json()["detail"]
    invented = copy.deepcopy(edit)
    invented["citations"].append({"chunk_id": "hb:c0", "quote": "replace the whole engine without inspection"})
    r = await env.decide(run_id, {"action": "edit", "draft_sha256": sha, "edited_draft": invented})
    assert r.status_code == 422 and "checked citations" in r.json()["detail"]


async def test_an_expired_run_is_refused_and_then_expired(env: Env) -> None:
    run_id, sha = await env.paused_run()
    with psycopg.connect(env.db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("UPDATE runs SET updated_at = now() - interval '8 days' WHERE run_id = %s", (run_id,))
    r = await env.decide(run_id, {"action": "approve", "draft_sha256": sha})
    assert r.status_code == 409 and "window" in r.json()["detail"]
    assert await expire_approvals(env.deps.rw_pool) == [run_id]
    assert run_row(env.db, run_id)["status"] == "expired"
    assert env.q("SELECT action, actor FROM audit_log") == [("run_expired", "system")]


async def test_a_run_on_an_unserved_graph_version_cannot_be_decided(env: Env) -> None:
    run_id, sha = await env.paused_run(graph_version="2")
    with psycopg.connect(env.db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("UPDATE runs SET graph_version = '0' WHERE run_id = %s", (run_id,))
    assert (await env.decide(run_id, {"action": "approve", "draft_sha256": sha})).status_code == 409


# ---- after the decision: the worker resumes the run ------------------------------------------------------


async def test_approve_writes_exactly_one_work_order_with_an_audit_chain(env: Env) -> None:
    run_id, sha = await env.paused_run()
    assert (await env.decide(run_id, {"action": "approve", "draft_sha256": sha})).status_code == 202
    assert await execute_run(run_id, env.deps) == "written"
    [(priority, created_by, approved_by, reg)] = env.q(
        "SELECT priority, created_by, approved_by, registration FROM work_orders WHERE run_id = %s", run_id
    )
    assert (created_by, approved_by, reg, priority) == ("tech-1", "sup-1", "MX-001", "urgent")
    assert [a for (a,) in env.q("SELECT action FROM audit_log ORDER BY id")] == ["decision", "work_order_written"]
    async with env.deps.rw_pool.connection() as conn:
        assert (await audit.verify(conn))[0]
    kinds = [k for k, _ in events(env.db, run_id)]
    assert kinds[-3:] == ["decision", "written", "status"] and events(env.db, run_id)[-1][1] == {"status": "written"}
    assert await execute_run(run_id, env.deps) == "skipped"  # a duplicate job does nothing


async def test_edit_writes_the_edited_draft(env: Env) -> None:
    run_id, sha = await env.paused_run()
    draft = (await env.http.get(f"/runs/{run_id}", headers=SUP)).json()["draft"]
    first_cite = {"chunk_id": draft["citations"][0]["chunk_id"], "quote": draft["citations"][0]["quote"]}
    edit = {"registration": "MX-001", "title": "Inspect chip detector, oil filter and strainer (edited)",
            "priority": "high", "diagnosis_summary": draft["diagnosis_summary"], "steps": draft["steps"][:2],
            "citations": [first_cite]}  # fmt: skip
    assert (await env.decide(run_id, {"action": "edit", "draft_sha256": sha, "edited_draft": edit})).status_code == 202
    assert await execute_run(run_id, env.deps) == "written"
    [(title, priority, cites)] = env.q("SELECT title, priority, citations FROM work_orders WHERE run_id = %s", run_id)
    assert (title, priority) == (edit["title"], "high")
    assert cites[0]["page_label"] == draft["citations"][0]["page_label"]  # the checked citation, not the bare edit


async def test_reject_writes_nothing_but_the_decision(env: Env) -> None:
    run_id, sha = await env.paused_run()
    body = {"action": "reject", "draft_sha256": sha, "reason": "Needs a borescope first"}
    assert (await env.decide(run_id, body)).status_code == 202
    assert await execute_run(run_id, env.deps) == "rejected"
    assert env.q("SELECT * FROM work_orders") == []
    assert [a for (a,) in env.q("SELECT action FROM audit_log ORDER BY id")] == ["decision", "work_order_rejected"]


async def test_the_write_is_idempotent_if_the_worker_dies_after_committing(env: Env) -> None:
    """A worker that dies after the write commits but before its checkpoint re-runs the write node."""
    run_id, sha = await env.paused_run()
    await env.decide(run_id, {"action": "approve", "draft_sha256": sha})
    graph_state = {"run_id": str(run_id), "draft": events(env.db, run_id)[-3][1]["draft"], "draft_sha256": sha}
    first = await write_work_order(env.deps.rw_pool, run_id, graph_state)
    again = await write_work_order(env.deps.rw_pool, run_id, graph_state)
    assert first[0] == again[0] and (first[1], again[1]) == (True, False)
    assert len(env.q("SELECT * FROM work_orders")) == 1
    assert len(env.q("SELECT * FROM audit_log WHERE action = 'work_order_written'")) == 1


async def test_a_run_paused_under_graph_v1_resumes_under_v2(env: Env) -> None:
    """DESIGN §6.3.1: v2 only added nodes after the interrupt, so v1 checkpoints stay resumable."""
    run_id, sha = await env.paused_run(graph_version="1")
    assert (await env.decide(run_id, {"action": "approve", "draft_sha256": sha})).status_code == 202
    assert await execute_run(run_id, env.deps) == "written"


async def test_the_draft_the_supervisor_saw_is_what_gets_written(env: Env) -> None:
    run_id, sha = await env.paused_run()
    shown = (await env.http.get("/approvals", headers=SUP)).json()[0]["draft"]
    await env.decide(run_id, {"action": "approve", "draft_sha256": sha})
    await execute_run(run_id, env.deps)
    [(title, steps)] = env.q("SELECT title, steps FROM work_orders WHERE run_id = %s", run_id)
    assert (title, steps) == (shown["title"], shown["steps"])
    assert draft_sha256(shown) == sha  # the hash the supervisor quotes is the hash of exactly what they saw
