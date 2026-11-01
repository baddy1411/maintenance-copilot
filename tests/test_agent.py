"""Agent graph and runner on real Postgres with a scripted model (only OpenRouter is faked). Needs Docker."""

import copy
from collections.abc import AsyncIterator
from uuid import UUID

import psycopg
import pytest
from pydantic import SecretStr

from app.agent.graph import check_citations, compact, draft_sha256
from app.agent.runner import RunDeps, execute_run
from app.agent.schemas import Citation
from app.config import Settings
from app.jobs import enqueue_run, make_app, reap
from app.llm import Gateway
from app.resources import agent_resources
from tests.agent_fixtures import DIAGNOSIS, DRAFT, SCRIPT_ACT, as_json, create_run, events, run_row, seed
from tests.fake_openrouter import FakeOpenRouter, MemoryLedger, completion, error, tool_call_completion

pytestmark = pytest.mark.integration


# ---- units ------------------------------------------------------------------------------------------------


def test_compact_condenses_old_tool_results_to_their_passage_ids() -> None:
    msgs = [{"role": "tool", "tool_call_id": str(i), "content": f"[hb:c{i}] Title\ntext {i}\n\n[ad:x:c{i}] AD"}
            for i in range(6)]  # fmt: skip
    out = compact(msgs, keep=4)
    assert out[0]["content"] == "(earlier result, condensed: passages hb:c0, ad:x:c0)"
    assert out[1]["content"].startswith("(earlier result")
    assert out[2:] == msgs[2:]


def test_draft_hash_is_canonical() -> None:
    a = {"title": "x", "steps": ["a"], "priority": "low"}
    assert draft_sha256(a) == draft_sha256(dict(reversed(list(a.items()))))
    assert draft_sha256(a) != draft_sha256({**a, "priority": "high"})


def test_citations_must_name_a_retrieved_passage_and_quote_it() -> None:
    ev = {"c1": {"doc_id": "d", "title": "t", "page": 1, "page_label": "1-1", "ad_number": None, "superseded_by": None,
                 "text": "Safety wire must be twisted tight before installation."}}  # fmt: skip
    ok, dropped = check_citations([Citation(chunk_id="c1", quote="safety wire must be twisted tight"),
                                   Citation(chunk_id="c9", quote="anything at all here"),
                                   Citation(chunk_id="c1", quote="safety wire may be left loose")], ev)  # fmt: skip
    assert [c["chunk_id"] for c in ok] == ["c1"]
    assert dropped == ["c9: not a retrieved passage", "c1: quote not found in passage"]


# ---- graph and runner -------------------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeOpenRouter:
    return FakeOpenRouter(embed_dim=1024)


@pytest.fixture
async def deps(db: Settings, fake: FakeOpenRouter) -> AsyncIterator[RunDeps]:
    seed(db)
    s = db.model_copy(update={"openrouter_api_key": SecretStr("sk-test"), "user_hash_salt": SecretStr("s"),
                              "llm_max_retries": 0})  # fmt: skip
    async with fake.client() as http, agent_resources(s, Gateway(s, http, MemoryLedger())) as d:
        yield d


def script(fake: FakeOpenRouter, act: list[list[tuple[str, dict]]] = SCRIPT_ACT, diagnosis: dict = DIAGNOSIS,
           draft: dict = DRAFT) -> None:  # fmt: skip
    for calls in act:
        fake.chat.append(tool_call_completion(calls) if calls else completion("Found the procedure and an AD."))
    fake.chat.extend([completion(as_json(diagnosis)), completion(as_json(draft))])


async def test_run_gathers_evidence_and_pauses_for_approval(db: Settings, deps: RunDeps, fake: FakeOpenRouter) -> None:
    script(fake)
    run_id = create_run(db)
    assert await execute_run(run_id, deps) == "pending_approval"
    kinds = [k for k, _ in events(db, run_id)]
    assert kinds == ["status", "act", "tools", "act", "tools", "act", "diagnosis", "draft", "status"]
    ev = dict(events(db, run_id))
    assert ev["status"]["status"] == "pending_approval"
    draft = ev["draft"]["draft"]
    assert draft["registration"] == "MX-001" and len(draft["citations"]) == 2
    row = run_row(db, run_id)
    assert row["status"] == "pending_approval" and row["draft_sha256"] == ev["draft"]["draft_sha256"]
    # The tool calls the model made reached the real tools and the real retriever.
    first_act = next(p for k, p in events(db, run_id) if k == "act")
    assert first_act["tool_calls"][0]["name"] == "get_aircraft"
    tools_events = [p for k, p in events(db, run_id) if k == "tools"]
    assert tools_events[1]["results"][0]["passages"] == 2


async def test_priority_is_raised_when_a_cited_ad_requires_action_before_flight(
    db: Settings, deps: RunDeps, fake: FakeOpenRouter
) -> None:
    script(fake)  # DRAFT says "medium" and cites AD 2026-99-01, whose passage says "before further flight"
    run_id = create_run(db)
    await execute_run(run_id, deps)
    draft = dict(events(db, run_id))["draft"]["draft"]
    assert draft["priority"] == "urgent"
    assert any("priority raised from medium to urgent" in c for c in draft["checks"])


async def test_a_different_registration_in_the_draft_is_corrected(
    db: Settings, deps: RunDeps, fake: FakeOpenRouter
) -> None:
    script(fake, draft={**DRAFT, "registration": "MX-002"})
    run_id = create_run(db)
    await execute_run(run_id, deps)
    draft = dict(events(db, run_id))["draft"]["draft"]
    assert draft["registration"] == "MX-001" and "registration corrected from MX-002 to MX-001" in draft["checks"]


async def test_fabricated_citations_are_dropped_and_flagged(db: Settings, deps: RunDeps, fake: FakeOpenRouter) -> None:
    bad = copy.deepcopy(DIAGNOSIS)
    bad["citations"] = [{"chunk_id": "hb:c0", "quote": "replace the engine after any metal is ever found"}]
    script(fake, diagnosis=bad)
    run_id = create_run(db)
    await execute_run(run_id, deps)
    diag = dict(events(db, run_id))["diagnosis"]["diagnosis"]
    assert diag["citations"] == [] and diag["evidence_incomplete"] is True
    assert diag["dropped_citations"] == ["hb:c0: quote not found in passage"]


async def test_step_cap_moves_on_to_diagnosis(db: Settings, fake: FakeOpenRouter) -> None:
    seed(db)
    s = db.model_copy(update={"openrouter_api_key": SecretStr("sk-test"), "agent_max_steps": 2, "llm_max_retries": 0})
    search = [("search_documents", {"query": "metal in oil", "top_k": 3})]
    script(fake, act=[search, search, search])  # the model never stops asking for tools
    async with fake.client() as http, agent_resources(s, Gateway(s, http, MemoryLedger())) as d:
        run_id = create_run(db)
        assert await execute_run(run_id, d) == "pending_approval"
    kinds = [k for k, _ in events(db, run_id)]
    assert kinds.count("tools") == 2 and "diagnosis" in kinds
    assert dict(events(db, run_id))["diagnosis"]["diagnosis"]["evidence_incomplete"] is True


async def test_unknown_aircraft_tool_result_says_not_in_fleet(
    db: Settings, deps: RunDeps, fake: FakeOpenRouter
) -> None:
    script(fake, act=[[("get_aircraft", {"registration": "MX-999"})], []])
    run_id = create_run(db)
    await execute_run(run_id, deps)
    act_msgs = [m for m in fake.bodies("/chat/completions")[1]["messages"] if m["role"] == "tool"]
    assert "MX-999 is not in the fleet. Do not use any other aircraft." in act_msgs[0]["content"]


async def test_credits_exhausted_fails_the_run_with_the_reason(
    db: Settings, deps: RunDeps, fake: FakeOpenRouter
) -> None:
    fake.chat.append(error(402, "requires more credits"))
    run_id = create_run(db)
    assert await execute_run(run_id, deps) == "failed"
    last = events(db, run_id)[-1]
    assert last[0] == "status" and last[1]["status"] == "failed" and "CreditsExhausted" in last[1]["reason"]


async def test_executing_a_paused_run_again_does_nothing(db: Settings, deps: RunDeps, fake: FakeOpenRouter) -> None:
    script(fake)
    run_id = create_run(db)
    await execute_run(run_id, deps)
    n = len(events(db, run_id))
    assert await execute_run(run_id, deps) == "skipped"
    assert len(events(db, run_id)) == n and run_row(db, run_id)["attempts"] == 1


async def test_crash_mid_run_resumes_from_the_last_checkpoint(
    db: Settings, deps: RunDeps, fake: FakeOpenRouter
) -> None:
    """In-process version: the model connection dies on the third act turn. The next execution resumes
    after the completed steps and doesn't redo them. The real two-process SIGKILL test is in
    test_agent_kill.py."""
    fake.chat.extend([tool_call_completion(SCRIPT_ACT[0]), tool_call_completion(SCRIPT_ACT[1])])
    run_id = create_run(db)

    class Boom(Exception):
        pass

    real_chat = deps.agent.gateway.chat
    calls = {"n": 0}

    async def flaky(*a, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 3:
            raise Boom("worker died")
        return await real_chat(*a, **kw)

    deps.agent.gateway.chat = flaky  # type: ignore[method-assign]
    with pytest.raises(Boom):
        await execute_run(run_id, deps)
    assert run_row(db, run_id)["status"] == "running"
    deps.agent.gateway.chat = real_chat  # type: ignore[method-assign]
    fake.chat.extend([completion("Found it."), completion(as_json(DIAGNOSIS)), completion(as_json(DRAFT))])
    sent_before = len(fake.bodies("/chat/completions"))
    assert await execute_run(run_id, deps) == "pending_approval"
    # Only the remaining steps ran: the final act, diagnose and draft. Not the two completed tool rounds.
    assert len(fake.bodies("/chat/completions")) - sent_before == 3
    statuses = [p for k, p in events(db, run_id) if k == "status"]
    assert statuses[1] == {"status": "running", "attempt": 2, "resumed": True}
    assert run_row(db, run_id)["attempts"] == 2


async def test_reaper_requeues_stale_runs_and_fails_hopeless_ones(db: Settings, deps: RunDeps) -> None:
    stale, hopeless, fresh = create_run(db), create_run(db), create_run(db)
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE procrastinate_jobs CASCADE")
        conn.execute("UPDATE runs SET status = 'running', attempts = 1, heartbeat_at = now() - interval '10 minutes' "
                     "WHERE run_id = %s", (stale,))  # fmt: skip
        conn.execute("UPDATE runs SET status = 'running', attempts = 3, heartbeat_at = now() - interval '10 minutes' "
                     "WHERE run_id = %s", (hopeless,))  # fmt: skip
        conn.execute(
            "UPDATE runs SET status = 'running', attempts = 1, heartbeat_at = now() WHERE run_id = %s", (fresh,)
        )
    app = make_app(deps.settings, role="app_rw")
    async with app.open_async():
        assert await reap(app, deps) == {"requeued": 1, "failed": 1, "already_queued": 0}
        # The "requeued" event refreshes the heartbeat, so the job gets a fresh window before the next check.
        assert await reap(app, deps) == {"requeued": 0, "failed": 0, "already_queued": 0}
        assert await enqueue_run(app, stale) is False  # the queueing lock: one queued job per run
    with psycopg.connect(db.dsn("migrator")) as conn:
        jobs = conn.execute("SELECT task_name, queue_name, args->>'run_id', lock FROM procrastinate_jobs").fetchall()
    assert jobs == [("run_agent", "agent", str(stale), f"run:{stale}")]
    assert run_row(db, hopeless)["status"] == "failed" and run_row(db, fresh)["status"] == "running"
    assert UUID(jobs[0][2]) == stale
