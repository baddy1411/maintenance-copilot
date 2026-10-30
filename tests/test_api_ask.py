"""POST /ask end to end: the real FastAPI app with its lifespan, real Postgres (seeded), the real gateway,
retriever and answer service. Only OpenRouter is faked. Needs Docker."""

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.api.auth import DevAuthNotAllowed
from app.api.main import create_app
from app.config import Settings
from app.ingest.store import halfvec
from app.llm import Gateway
from app.qa.answer import DELIMITER
from tests.fake_openrouter import VOYAGE, FakeOpenRouter, MemoryLedger, error, sse_completion

pytestmark = pytest.mark.integration

CHUNKS = [  # (doc_id, source, access, page, label, text)
    ("faa-amt-powerplant", "handbook", "public", 329, "8-2",
     "A chip detector is a magnetic plug in the oil system that collects ferrous metal particles from the oil. "
     "Metal on the detector or in the oil filter calls for further inspection before further flight."),
    ("faa-ac-43-13-1b", "ac", "public", 330, "7-21",
     "Safety wire must be twisted tight and installed so that it tends to tighten the fastener."),
    ("upload:secret", "upload", "restricted", 1, "S-1",
     "Restricted shop procedure: chip detector secret torque value 42 in-lb."),
]  # fmt: skip
ANSWER = ("A chip detector is a magnetic plug that collects ferrous metal particles from the oil [1]. Metal on the "
          "detector calls for further inspection before further flight [1].")  # fmt: skip
SOURCES = '[1] "a magnetic plug in the oil system that collects ferrous metal particles"'


@dataclass
class Client:
    http: httpx2.AsyncClient
    openrouter: FakeOpenRouter
    ledger: MemoryLedger

    async def ask(self, question: str, user: str = "tech-7") -> list[tuple[str, Any]]:
        r = await self.http.post("/ask", json={"question": question}, headers={"X-Dev-User": user})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        events = []
        for block in r.text.split("\n\n"):
            fields = dict(
                line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":")
            )
            if "event" in fields:
                events.append((fields["event"], json.loads(fields["data"])))
        return events


@pytest.fixture
def seeded(db: Settings) -> Iterator[None]:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE documents, chunks, chunk_embeddings CASCADE")
        for doc_id, source, access, page, label, text in CHUNKS:
            conn.execute("INSERT INTO documents (doc_id, source, ingest_origin, title, sha256, access_level) "
                         "VALUES (%s, %s, %s, %s, 'x', %s)",
                         (doc_id, source, "admin_upload" if source == "upload" else "official", doc_id,
                          access))  # fmt: skip
            conn.execute("INSERT INTO chunks (chunk_id, doc_id, page, page_label, ord, text) VALUES (%s,%s,%s,%s,0,%s)",
                         (f"{doc_id}:c0", doc_id, page, label, text))  # fmt: skip
            conn.execute("INSERT INTO chunk_embeddings VALUES (%s, %s, %s::halfvec)",
                         (f"{doc_id}:c0", VOYAGE, halfvec([1.0] * 1024)))  # fmt: skip
    yield


@pytest.fixture
async def client(db: Settings, seeded: None) -> AsyncIterator[Client]:
    s = db.model_copy(update={"openrouter_api_key": SecretStr("sk-test"), "user_hash_salt": SecretStr("s"),
                              "embed_model": VOYAGE, "llm_max_retries": 0})  # fmt: skip
    fake, ledger = FakeOpenRouter(embed_dim=1024), MemoryLedger()
    async with fake.client() as llm_http:
        app: FastAPI = create_app(s, Gateway(s, llm_http, ledger))
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http,
        ):
            yield Client(http, fake, ledger)


async def test_answer_streams_then_cites_with_verified_quotes(client: Client) -> None:
    client.openrouter.chat.append(sse_completion(["A chip detector is a magnetic ", ANSWER[30:80], ANSWER[80:],
                                                  "\n" + DELIMITER[:6], DELIMITER[6:] + "\n", SOURCES]))  # fmt: skip
    events = await client.ask("What does a chip detector do?")
    kinds = [e for e, _ in events]
    assert kinds[:2] == ["status", "status"] and kinds[-2:] == ["citations", "done"]
    assert events[0][1] == {"stage": "searching"}
    text = "".join(d["text"] for e, d in events if e == "token")
    assert text.strip() == ANSWER and DELIMITER not in text
    citations = dict(events)["citations"]["items"]
    assert [(c["n"], c["doc_id"], c["page_label"]) for c in citations] == [(1, "faa-amt-powerplant", "8-2")]
    done = events[-1][1]
    assert done["verdict"] == "answered" and done["unsupported"] == 0 and done["cost"] == 0.0006
    assert done["timings_ms"]["first_token"] <= done["timings_ms"]["total"]


async def test_restricted_text_never_reaches_the_model_for_a_public_user(client: Client) -> None:
    client.openrouter.chat.append(sse_completion([ANSWER, "\n" + DELIMITER + "\n" + SOURCES]))
    await client.ask("What is the chip detector torque value?")
    prompt = json.dumps(client.openrouter.bodies("/chat/completions")[0]["messages"])
    reranked = json.dumps(client.openrouter.bodies("/rerank"))
    assert "secret torque" not in prompt and "secret torque" not in reranked


async def test_low_relevance_is_refused_without_calling_the_model(client: Client) -> None:
    client.openrouter.rerank_top = 0.3
    events = await client.ask("How do I bake sourdough bread?")
    assert [e for e, _ in events] == ["status", "done"]
    assert events[-1][1]["verdict"] == "not_found" and "FAA sources" in events[-1][1]["message"]
    assert client.openrouter.bodies("/chat/completions") == []


async def test_model_not_found_is_a_refusal_and_never_shown(client: Client) -> None:
    client.openrouter.chat.append(sse_completion(["NOT_", "FOUND"]))
    events = await client.ask("What is the torque for a Boeing 797 wheel nut?")
    assert not [e for e, _ in events if e == "token"]
    assert events[-1][1]["verdict"] == "not_found"


async def test_fabricated_quotes_lead_to_a_refusal(client: Client) -> None:
    fabricated = '[1] "replace the engine immediately after any metal is found"'
    client.openrouter.chat.append(sse_completion([ANSWER, "\n" + DELIMITER + "\n", fabricated]))
    events = await client.ask("What does a chip detector do?")
    data = dict(events)
    assert data["citations"]["items"] == [] and "quote not found" in data["citations"]["dropped"][0]
    assert events[-1][1]["verdict"] == "refused_unsupported" and events[-1][1]["message"]


async def test_model_down_returns_the_passages(client: Client) -> None:
    client.openrouter.chat.extend([error(503, "Provider returned error"), error(503, "Provider returned error")])
    events = await client.ask("What does a chip detector do?")
    data = dict(events)
    assert data["done"]["verdict"] == "generation_unavailable" and data["done"]["reason"] == "ModelUnavailable"
    docs = {c["doc_id"] for c in data["citations"]["items"]}
    assert "faa-amt-powerplant" in docs and "upload:secret" not in docs


@pytest.mark.parametrize("question", ["", "hi", "x" * 2001])
async def test_question_length_is_validated(client: Client, question: str) -> None:
    r = await client.http.post("/ask", json={"question": question})
    assert r.status_code == 422


async def test_spend_is_recorded_per_user(client: Client) -> None:
    client.openrouter.chat.append(sse_completion([ANSWER, "\n" + DELIMITER + "\n" + SOURCES]))
    await client.ask("What does a chip detector do?", user="tech-42")
    kinds = {r.ctx.kind for r in client.ledger.records}
    assert {"retrieval", "answer"} <= kinds
    assert all(r.user_hash and "tech-42" not in r.user_hash for r in client.ledger.records)


def test_dev_auth_is_refused_outside_local(db: Settings) -> None:
    with pytest.raises(DevAuthNotAllowed):
        create_app(db.model_copy(update={"env": "prod"}))
