"""AD sync end to end on real Postgres, with a fake Federal Register + govinfo and a fake OpenRouter.
Needs Docker."""

import json
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from app.config import Settings
from app.ingest.ads import sync_ads
from app.ingest.embed_missing import embed_missing
from app.ingest.fetch import SafeFetcher, allowed
from app.llm import Gateway, PostgresLedger
from tests.fake_openrouter import VOYAGE, FakeOpenRouter

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parent / "fixtures" / "ads"


def fr_doc(fixture: str, number: str, published: str, *, ad_number: str | None = None) -> dict[str, Any]:
    """A Federal Register result built from a real AD fixture, renumbered so tests don't collide."""
    d = json.loads((FIXTURES / f"{fixture}.json").read_text())
    text = d["text"]
    if ad_number:  # rewrite the header's AD number, e.g. to make a synthetic original for the R1 fixture
        text = re.sub(r"(Amendment\s+39-\d+;\s*AD\s+)[\d-]+(R\d+)?\]", rf"\g<1>{ad_number}]", text, count=1)
    return {"document_number": number, "title": d["title"], "publication_date": published,
            "effective_on": "2026-12-01", "html_url": "https://www.federalregister.gov/x",
            "pdf_url": f"https://www.govinfo.gov/content/pkg/FR-{published}/pdf/{number}.pdf",
            "_text": text}  # fmt: skip


@dataclass
class FakeFR:
    docs: list[dict[str, Any]] = field(default_factory=list)
    govinfo_override: dict[str, httpx2.Response] = field(default_factory=dict)
    text_fetches: int = 0

    def handler(self, req: httpx2.Request) -> httpx2.Response:
        if req.url.host == "www.federalregister.gov":
            q = parse_qs(urlsplit(str(req.url)).query)
            lo, hi = q["conditions[publication_date][gte]"][0], q["conditions[publication_date][lte]"][0]
            hits = sorted((d for d in self.docs if lo <= d["publication_date"] <= hi),
                          key=lambda d: d["publication_date"])  # fmt: skip
            results = [{k: v for k, v in d.items() if not k.startswith("_")} for d in hits]
            return httpx2.Response(200, json={"count": len(results), "total_pages": 1, "results": results})
        number = req.url.path.rsplit("/", 1)[-1].removesuffix(".htm")
        self.text_fetches += 1
        if number in self.govinfo_override:
            return self.govinfo_override[number]
        doc = next(d for d in self.docs if d["document_number"] == number)
        return httpx2.Response(200, text=f"<html><body><pre>{doc['_text']}</pre></body></html>")


@dataclass
class Env:
    settings: Settings
    pool: AsyncConnectionPool
    fetcher: SafeFetcher
    gateway: Gateway
    fr: FakeFR
    openrouter: FakeOpenRouter

    async def sync(self, **kw: Any) -> Any:
        return await sync_ads(self.settings, self.fetcher, self.pool, kw.pop("gateway", self.gateway), **kw)

    async def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()


@pytest.fixture
def clean(db: Settings) -> Iterator[None]:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE documents, chunks, chunk_embeddings, quarantine, sync_state, ingest_runs CASCADE")
    yield


@pytest.fixture
async def env(db: Settings, clean: None) -> AsyncIterator[Env]:
    # model_copy skips validation, so secrets must already be SecretStr.
    s = db.model_copy(
        update={"openrouter_api_key": SecretStr("sk-test"), "user_hash_salt": SecretStr("s"), "embed_model": VOYAGE}
    )
    fr, orouter = FakeFR(), FakeOpenRouter(embed_dim=1024)

    async def resolver(host: str) -> list[str]:
        return ["23.10.1.1"]

    async with (
        AsyncConnectionPool(db.dsn("ingest_rw"), min_size=1, max_size=2, open=False) as pool,
        httpx2.AsyncClient(transport=httpx2.MockTransport(fr.handler)) as fetch_http,
        orouter.client() as llm_http,
    ):
        fetcher = SafeFetcher(fetch_http, allowed(s.ingest_allowed_hosts), "test", resolver=resolver,
                              min_interval_s=0, attempts=1)  # fmt: skip
        yield Env(s, pool, fetcher, Gateway(s, llm_http, PostgresLedger(pool)), fr, orouter)


async def test_sync_stores_ads_chunks_vectors_and_metadata(env: Env) -> None:
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01"), fr_doc("model_range", "2020-28270", "2020-12-21"),
                   fr_doc("correction", "2021-20404", "2021-09-20")]  # fmt: skip
    r = await env.sync(since=date(2020, 1, 1), until=date(2021, 12, 31))
    assert (r.fetched, r.inserted, r.quarantined, r.failed, r.stopped_early) == (3, 3, 0, 0, None)
    docs = await env.rows("SELECT doc_id, ad_number, doc_kind, make, models[1], access_level::text "
                          "FROM documents ORDER BY doc_id")  # fmt: skip
    assert docs == [
        ("ad:2020-18829", "2020-18-08", "original", ["Robinson Helicopter Company"], "R44", "public"),
        ("ad:2020-28270", "2020-26-09", "original", ["The Boeing Company"], "737-100", "public"),
        ("ad:2021-20404", "2021-17-14", "correction", ["Gulfstream Aerospace LP"], "G280", "public"),
    ]
    [(n_chunks, n_vectors, n_prefixed)] = await env.rows(
        "SELECT count(*), count(e.chunk_id), count(*) FILTER (WHERE c.text LIKE '[AD %%') "
        "FROM chunks c LEFT JOIN chunk_embeddings e USING (chunk_id)")  # fmt: skip
    assert n_chunks > 10 and n_vectors == n_chunks == n_prefixed
    [(hwm,)] = await env.rows("SELECT high_water_mark FROM sync_state WHERE source = 'ads'")
    assert hwm == date(2021, 9, 20)
    [(status, fetched, inserted, cost)] = await env.rows(
        "SELECT status, fetched, inserted, cost::float FROM ingest_runs"
    )
    assert (status, fetched, inserted) == ("succeeded", 3, 3) and cost > 0
    [(kinds,)] = await env.rows("SELECT array_agg(DISTINCT kind) FROM llm_calls WHERE kind = 'embed_corpus'")
    assert kinds == ["embed_corpus"]


async def test_rerun_skips_unchanged_documents_without_paying_again(env: Env) -> None:
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01")]
    await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    embeds_before = len(env.openrouter.bodies("/embeddings"))
    r = await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    assert (r.unchanged, r.inserted, r.updated, r.cost) == (1, 0, 0, 0.0)
    assert len(env.openrouter.bodies("/embeddings")) == embeds_before


async def test_supersession_is_linked_in_both_orders(env: Env) -> None:
    original = fr_doc("model_range", "2020-11111", "2020-06-01", ad_number="2020-25-03")
    revision = fr_doc("revision", "2023-00185", "2023-01-12")
    # Revision first (out-of-order backfill), then the original it replaces.
    env.fr.docs = [revision]
    await env.sync(since=date(2023, 1, 1), until=date(2023, 12, 31))
    env.fr.docs = [revision, original]
    await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    rows = await env.rows("SELECT ad_number, doc_kind, superseded_by FROM documents ORDER BY ad_number")
    assert rows == [("2020-25-03", "original", "2020-25-03R1"), ("2020-25-03R1", "revision", None)]


async def test_block_page_is_quarantined_not_stored(env: Env) -> None:
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01"), fr_doc("model_range", "2020-28270", "2020-10-01")]
    env.fr.govinfo_override["2020-28270"] = httpx2.Response(200, text="<h1>Request Access</h1><p>bot check</p>")
    r = await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    assert (r.inserted, r.quarantined) == (1, 1)
    assert await env.rows("SELECT doc_id FROM documents") == [("ad:2020-18829",)]
    await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    [(reason, attempts)] = await env.rows("SELECT reason, attempts FROM quarantine")
    assert (reason, attempts) == ("block page", 2)


async def test_failed_fetch_holds_the_high_water_mark_before_it(env: Env) -> None:
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01"), fr_doc("model_range", "2020-28270", "2020-10-01"),
                   fr_doc("correction", "2021-20404", "2020-11-01")]  # fmt: skip
    env.fr.govinfo_override["2020-28270"] = httpx2.Response(503)
    env.settings = env.settings.model_copy(update={"ad_backfill_from": date(2020, 1, 1)})
    r = await env.sync(until=date(2020, 12, 31))  # scheduled-style run: no `since`
    assert (r.inserted, r.failed) == (2, 1)
    [(hwm,)] = await env.rows("SELECT high_water_mark FROM sync_state WHERE source = 'ads'")
    assert hwm == date(2020, 9, 30)  # the day before the failed document, so the next run re-reads it
    [(status,)] = await env.rows("SELECT status FROM ingest_runs")
    assert status == "failed"


async def test_job_cap_stops_before_spending_and_commits_nothing(env: Env) -> None:
    env.settings = env.settings.model_copy(update={"ingest_job_cap_usd": 0.0})
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01")]
    r = await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    assert r.stopped_early and "job cap" in r.stopped_early
    assert env.openrouter.bodies("/embeddings") == []
    assert await env.rows("SELECT * FROM documents") == []
    assert await env.rows("SELECT * FROM sync_state") == []


async def test_fts_only_sync_then_embed_missing(env: Env) -> None:
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01")]
    await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31), gateway=None)
    [(n_chunks, n_vectors)] = await env.rows(
        "SELECT count(*), count(e.chunk_id) FROM chunks c LEFT JOIN chunk_embeddings e USING (chunk_id)"
    )
    assert n_chunks > 0 and n_vectors == 0
    first = await embed_missing(env.pool, env.gateway, VOYAGE, cap_usd=1.0, limit=3)
    assert first.embedded == 3
    rest = await embed_missing(env.pool, env.gateway, VOYAGE, cap_usd=1.0)
    assert rest.embedded == n_chunks - 3
    assert (await embed_missing(env.pool, env.gateway, VOYAGE, cap_usd=1.0)).embedded == 0
    [(n,)] = await env.rows("SELECT count(*) FROM chunk_embeddings")
    assert n == n_chunks


async def test_tools_role_sees_ads_through_the_view(env: Env, db: Settings) -> None:
    env.fr.docs = [fr_doc("model_range", "2020-28270", "2020-12-21")]
    await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    with psycopg.connect(db.dsn("tools_ro")) as conn:
        hits = conn.execute(
            "SELECT ad_number, doc_kind FROM tools_chunks WHERE tsv @@ plainto_tsquery('english', 'Boeing 737') "
            "LIMIT 1").fetchall()  # fmt: skip
    assert hits == [("2020-26-09", "original")]


async def test_nul_bytes_from_govinfo_are_stored_cleanly(env: Env) -> None:
    doc = fr_doc("supersedes", "2020-07399", "2020-04-09")
    doc["_text"] = doc["_text"].replace("\n\n", "\n\n\x00", 1)
    env.fr.docs = [doc]
    r = await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    assert (r.inserted, r.failed) == (1, 0)


async def test_parser_version_bump_reprocesses_stored_documents(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.fr.docs = [fr_doc("supersedes", "2020-18829", "2020-09-01")]
    await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    monkeypatch.setattr("app.ingest.ads.PARSER_VERSION", "test-next")
    r = await env.sync(since=date(2020, 1, 1), until=date(2020, 12, 31))
    assert (r.updated, r.unchanged) == (1, 0)
