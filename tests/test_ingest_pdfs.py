"""Official handbook and admin-upload ingestion on real Postgres, with generated PDFs. Needs Docker."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from app.config import Settings
from app.ingest.fetch import SafeFetcher, allowed
from app.ingest.pdf_docs import OFFICIAL_PDFS, ingest_official, ingest_upload
from app.llm import Gateway, PostgresLedger
from tests.fake_openrouter import VOYAGE, FakeOpenRouter
from tests.pdf_factory import handbook_page, make_pdf

pytestmark = pytest.mark.integration


@dataclass
class Env:
    settings: Settings
    pool: AsyncConnectionPool
    fetcher: SafeFetcher
    gateway: Gateway
    openrouter: FakeOpenRouter
    served: dict[str, bytes]

    async def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()


@pytest.fixture
def clean(db: Settings) -> Iterator[None]:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE documents, chunks, chunk_embeddings, quarantine, ingest_runs CASCADE")
    yield


@pytest.fixture
async def env(db: Settings, clean: None, tmp_path: Path) -> AsyncIterator[Env]:
    s = db.model_copy(update={"openrouter_api_key": SecretStr("sk-test"), "user_hash_salt": SecretStr("s"),
                              "embed_model": VOYAGE, "upload_dir": tmp_path / "uploads",
                              "pdf_wall_s": 30.0})  # fmt: skip
    # faa.gov serves one generated PDF per official document.
    served = {spec.url: make_pdf([handbook_page(spec.doc_id, "1-1"), handbook_page(spec.doc_id, "1-2")])
              for spec in OFFICIAL_PDFS}  # fmt: skip

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=served[str(req.url)], headers={"content-type": "application/pdf"})

    async def resolver(host: str) -> list[str]:
        return ["23.10.1.1"]

    orouter = FakeOpenRouter(embed_dim=1024)
    async with (
        AsyncConnectionPool(db.dsn("ingest_rw"), min_size=1, max_size=2, open=False) as pool,
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as fetch_http,
        orouter.client() as llm_http,
    ):
        fetcher = SafeFetcher(fetch_http, allowed(s.ingest_allowed_hosts), "test", resolver=resolver,
                              min_interval_s=0, attempts=1)  # fmt: skip
        yield Env(s, pool, fetcher, Gateway(s, llm_http, PostgresLedger(pool)), orouter, served)


async def test_official_pdfs_are_stored_public_with_page_labels(env: Env) -> None:
    r = await ingest_official(env.settings, env.fetcher, env.pool, env.gateway)
    assert (r.fetched, r.inserted, r.quarantined, r.failed) == (4, 4, 0, 0)
    docs = await env.rows("SELECT doc_id, source, ingest_origin, access_level::text FROM documents ORDER BY doc_id")
    assert docs == [("faa-ac-43-13-1b", "ac", "official", "public"),
                    ("faa-amt-airframe", "handbook", "official", "public"),
                    ("faa-amt-general", "handbook", "official", "public"),
                    ("faa-amt-powerplant", "handbook", "official", "public")]  # fmt: skip
    labels = await env.rows("SELECT DISTINCT page, page_label FROM chunks WHERE doc_id = 'faa-amt-airframe' ORDER BY 1")
    assert labels == [(1, "1-1"), (2, "1-2")]
    [(n, vectors)] = await env.rows("SELECT count(*), (SELECT count(*) FROM chunk_embeddings) FROM chunks")
    assert n == vectors > 0
    [(status,)] = await env.rows("SELECT status FROM ingest_runs")
    assert status == "succeeded"


async def test_unchanged_official_pdfs_are_skipped_without_parsing_or_spend(env: Env) -> None:
    await ingest_official(env.settings, env.fetcher, env.pool, env.gateway)
    embeds = len(env.openrouter.bodies("/embeddings"))
    r = await ingest_official(env.settings, env.fetcher, env.pool, env.gateway)
    assert (r.unchanged, r.inserted, r.updated, r.cost) == (4, 0, 0, 0.0)
    assert len(env.openrouter.bodies("/embeddings")) == embeds


async def test_only_selected_official_pdfs(env: Env) -> None:
    r = await ingest_official(env.settings, env.fetcher, env.pool, env.gateway, only={"faa-ac-43-13-1b"})
    assert (r.fetched, r.inserted) == (1, 1)
    assert await env.rows("SELECT doc_id FROM documents") == [("faa-ac-43-13-1b",)]


async def test_a_changed_official_pdf_is_reingested(env: Env) -> None:
    await ingest_official(env.settings, env.fetcher, env.pool, env.gateway, only={"faa-amt-general"})
    url = next(s.url for s in OFFICIAL_PDFS if s.doc_id == "faa-amt-general")
    env.served[url] = make_pdf([handbook_page("revised", "2-1")])
    r = await ingest_official(env.settings, env.fetcher, env.pool, env.gateway, only={"faa-amt-general"})
    assert r.updated == 1
    assert await env.rows("SELECT DISTINCT page_label FROM chunks WHERE doc_id = 'faa-amt-general'") == [("2-1",)]


async def test_upload_is_restricted_and_stored_under_its_hash(env: Env, tmp_path: Path) -> None:
    import hashlib

    data = make_pdf([handbook_page("shop", "A-1")])
    src = tmp_path / "Shop manual (scan) v2.pdf"  # the user's filename must not reach storage
    src.write_bytes(data)
    r, doc_id = await ingest_upload(env.settings, env.pool, env.gateway, src, "Shop manual")
    sha = hashlib.sha256(data).hexdigest()
    assert r.inserted == 1 and doc_id == f"upload:{sha[:16]}"
    [(source, origin, access, url)] = await env.rows(
        "SELECT source, ingest_origin, access_level::text, url FROM documents WHERE doc_id = %s", doc_id
    )
    assert (source, origin, access, url) == ("upload", "admin_upload", "restricted", None)
    assert [p.name for p in env.settings.upload_dir.iterdir()] == [f"{sha}.pdf"]
    again, _ = await ingest_upload(env.settings, env.pool, env.gateway, src, "Shop manual")
    assert again.unchanged == 1


async def test_non_pdf_upload_is_quarantined(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "invoice.pdf"
    src.write_bytes(b"MZ\x90\x00 this is an executable, not a PDF")
    r, doc_id = await ingest_upload(env.settings, env.pool, env.gateway, src, "Totally a manual")
    assert (r.inserted, r.quarantined) == (0, 1)
    assert await env.rows("SELECT * FROM documents") == []
    [(reason,)] = await env.rows("SELECT reason FROM quarantine WHERE url = %s", doc_id)
    assert reason == "not a PDF (magic bytes)"
    assert not env.settings.upload_dir.exists() or not any(env.settings.upload_dir.iterdir())


async def test_pdf_without_text_is_quarantined(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "scan.pdf"
    src.write_bytes(make_pdf([[], []]))  # image-only scans look like this to a text extractor
    r, _ = await ingest_upload(env.settings, env.pool, env.gateway, src, "Scanned manual")
    assert r.quarantined == 1
    [(reason,)] = await env.rows("SELECT reason FROM quarantine")
    assert reason.startswith("no extractable text")


async def test_restricted_upload_is_visible_to_tools_only_with_its_access_level(env: Env, db: Settings,
                                                                                tmp_path: Path) -> None:  # fmt: skip
    src = tmp_path / "m.pdf"
    src.write_bytes(make_pdf([handbook_page("gearbox", "G-1")]))
    _, doc_id = await ingest_upload(env.settings, env.pool, env.gateway, src, "Gearbox manual")
    with psycopg.connect(db.dsn("tools_ro")) as conn:
        rows = conn.execute("SELECT DISTINCT access_level::text FROM tools_chunks WHERE doc_id = %s",
                            (doc_id,)).fetchall()  # fmt: skip
    # The view exposes the level; retrieval (#8) filters on it inside the SQL, before rerank (DESIGN §7.2).
    assert rows == [("restricted",)]
