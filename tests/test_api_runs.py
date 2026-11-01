"""Agent runs over HTTP: create, dedupe, ownership, and the re-attachable event stream. Needs Docker."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import psycopg
import pytest
from pydantic import SecretStr

from app.agent.graph import GRAPH_VERSION
from app.agent.runner import execute_run
from app.api.main import create_app
from app.config import Settings
from app.llm import Gateway
from app.resources import agent_resources
from tests.agent_fixtures import seed
from tests.auth_helpers import bearer, with_auth
from tests.fake_openrouter import FakeOpenRouter, MemoryLedger
from tests.test_agent import script

pytestmark = pytest.mark.integration
BODY = {"registration": "MX-001", "symptom": "Shiny metal in drained oil after a flight"}


@dataclass
class Api:
    http: httpx2.AsyncClient
    settings: Settings
    fake: FakeOpenRouter

    async def post(self, body: dict[str, Any], key: str | None = "k-1", user: str = "tech-1") -> httpx2.Response:
        headers = bearer(user) | ({"Idempotency-Key": key} if key else {})
        return await self.http.post("/runs", json=body, headers=headers)

    async def stream(self, run_id: str, user: str = "tech-1", **headers: str) -> list[tuple[str | None, str, Any]]:
        r = await self.http.get(f"/runs/{run_id}/events", headers={**bearer(user), **headers})
        assert r.status_code == 200, r.text
        out = []
        for block in r.text.split("\n\n"):
            f = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
            if "event" in f:
                out.append((f.get("id"), f["event"], json.loads(f["data"])))
        return out


@pytest.fixture
async def api(db: Settings) -> AsyncIterator[Api]:
    seed(db)
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE procrastinate_jobs CASCADE")
    s = with_auth(db, openrouter_api_key=SecretStr("sk-test"), llm_max_retries=0)
    fake = FakeOpenRouter(embed_dim=1024)
    async with fake.client() as llm_http:
        app = create_app(s, Gateway(s, llm_http, MemoryLedger()))
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http,
        ):
            yield Api(http, s, fake)


def jobs(db: Settings) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.dsn("migrator")) as conn:
        return conn.execute("SELECT task_name, queue_name, args->>'run_id', status FROM procrastinate_jobs").fetchall()


async def test_create_queues_a_job_and_a_first_event(api: Api, db: Settings) -> None:
    r = await api.post(BODY)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert r.json() == {"run_id": run_id, "status": "queued", "events_url": f"/runs/{run_id}/events"}
    assert jobs(db) == [("run_agent", "agent", run_id, "todo")]
    got = (await api.http.get(f"/runs/{run_id}", headers=bearer("tech-1"))).json()
    assert got["status"] == "queued" and got["graph_version"] == GRAPH_VERSION and got["state_schema_version"] == 1


async def test_retry_with_the_same_key_returns_the_same_run(api: Api, db: Settings) -> None:
    first = (await api.post(BODY)).json()["run_id"]
    again = await api.post(BODY)
    assert again.status_code == 200 and again.json()["run_id"] == first
    assert len(jobs(db)) == 1


async def test_same_key_different_request_is_a_conflict(api: Api) -> None:
    await api.post(BODY)
    r = await api.post({**BODY, "symptom": "Something else entirely"})
    assert r.status_code == 409


async def test_keys_are_scoped_per_user(api: Api, db: Settings) -> None:
    a = (await api.post(BODY, user="tech-1")).json()["run_id"]
    b = (await api.post(BODY, user="tech-2")).json()["run_id"]
    assert a != b and len(jobs(db)) == 2


@pytest.mark.parametrize(("body", "code"), [({**BODY, "registration": "MX-999"}, 404),
                                             ({**BODY, "registration": "N12345"}, 422),
                                             ({**BODY, "symptom": "x"}, 422)])  # fmt: skip
async def test_bad_requests_are_rejected_before_anything_is_spent(
    api: Api, db: Settings, body: dict, code: int
) -> None:
    assert (await api.post(body)).status_code == code
    assert jobs(db) == []


async def test_another_users_run_is_not_found(api: Api) -> None:
    run_id = (await api.post(BODY, user="tech-1")).json()["run_id"]
    assert (await api.http.get(f"/runs/{run_id}", headers=bearer("tech-2"))).status_code == 404
    assert (await api.http.get(f"/runs/{run_id}/events", headers=bearer("tech-2"))).status_code == 404


async def test_event_stream_replays_and_resumes_from_last_event_id(api: Api) -> None:
    run_id = (await api.post(BODY)).json()["run_id"]
    script(api.fake)
    # The worker's side: execute the run with the same scripted model.
    async with api.fake.client() as http:
        gateway = Gateway(api.settings, http, MemoryLedger())
        async with agent_resources(api.settings, gateway) as deps:
            assert await execute_run(UUID(run_id), deps) == "pending_approval"

    full = await api.stream(run_id)
    kinds = [k for _, k, _ in full]
    assert kinds == ["status", "status", "act", "tools", "act", "tools", "act", "diagnosis", "draft", "status", "end"]
    ids = [i for i, _, _ in full if i]
    assert ids == [str(n) for n in range(1, len(ids) + 1)]  # SSE ids are the sequence numbers
    assert full[-1][2] == {"status": "pending_approval"}

    tail = await api.stream(run_id, **{"Last-Event-ID": "7"})
    assert [k for _, k, _ in tail] == ["diagnosis", "draft", "status", "end"]
    draft = (await api.http.get(f"/runs/{run_id}", headers=bearer("tech-1"))).json()["draft"]
    assert draft["registration"] == "MX-001"


async def test_stream_waits_for_the_final_event_not_just_the_status_column(api: Api, db: Settings) -> None:
    """Regression (found live in #12): the write node commits runs.status = 'written' a moment before the
    runner records the final events. A stream that ended on the status column cut them off."""
    import asyncio

    run_id = (await api.post(BODY)).json()["run_id"]
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("UPDATE runs SET status = 'written' WHERE run_id = %s", (run_id,))

    async def finish_late() -> None:
        await asyncio.sleep(0.8)
        with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
            for seq, kind, payload in [(2, "written", {"work_order_id": "w-1"}), (3, "status", {"status": "written"})]:
                conn.execute("INSERT INTO run_events (run_id, seq, kind, payload) VALUES (%s, %s, %s, %s)",
                             (run_id, seq, kind, json.dumps(payload)))  # fmt: skip

    late = asyncio.create_task(finish_late())
    got = await api.stream(run_id)
    await late
    assert [k for _, k, _ in got] == ["status", "written", "status", "end"]
