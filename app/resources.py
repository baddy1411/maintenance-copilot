"""Long-lived clients and pools, built from Settings. One place, so every process wires them the same way."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx2
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.ingest.fetch import SafeFetcher, allowed
from app.llm import Gateway, PostgresLedger


@dataclass(frozen=True)
class IngestResources:
    pool: AsyncConnectionPool  # ingest_rw
    fetcher: SafeFetcher
    gateway: Gateway | None


@asynccontextmanager
async def ingest_resources(settings: Settings, *, embed: bool = True) -> AsyncIterator[IngestResources]:
    async with (
        AsyncConnectionPool(settings.dsn("ingest_rw"), min_size=1, max_size=4, open=False) as pool,
        httpx2.AsyncClient() as fetch_http,
        httpx2.AsyncClient() as llm_http,
    ):
        fetcher = SafeFetcher(fetch_http, allowed(settings.ingest_allowed_hosts), settings.ingest_user_agent)
        gateway = Gateway(settings, llm_http, PostgresLedger(pool)) if embed else None
        yield IngestResources(pool, fetcher, gateway)
