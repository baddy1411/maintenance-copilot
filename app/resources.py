"""Long-lived clients and pools, built from Settings. One place, so every process wires them the same way."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

import httpx2
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import AgentDeps
from app.agent.runner import CheckpointPool, RunDeps
from app.agent.tools import Tools
from app.config import Settings
from app.ingest.fetch import SafeFetcher, allowed
from app.llm import Gateway, PostgresLedger
from app.retrieval.search import RetrievalConfig, Retriever


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


def checkpointer_pool(settings: Settings, max_size: int = 4) -> CheckpointPool:
    """What langgraph-checkpoint-postgres requires of its connections: autocommit, dict rows, and no
    server-side prepared statements (prepare_threshold=0)."""
    kwargs = {"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0}
    pool = AsyncConnectionPool(settings.dsn("app_rw"), min_size=1, max_size=max_size, open=False, kwargs=kwargs)
    return cast(CheckpointPool, pool)


@asynccontextmanager
async def agent_resources(settings: Settings, gateway: Gateway | None = None) -> AsyncIterator[RunDeps]:
    """Everything a worker needs to execute agent runs. `gateway` can be injected (tests)."""
    async with (
        AsyncConnectionPool(settings.dsn("app_rw"), min_size=1, max_size=4, open=False) as rw,
        AsyncConnectionPool(settings.dsn("tools_ro"), min_size=1, max_size=4, open=False) as ro,
        checkpointer_pool(settings) as cp,
        httpx2.AsyncClient() as http,
    ):
        gw = gateway or Gateway(settings, http, PostgresLedger(rw))
        tools = Tools(Retriever(ro, gw, RetrievalConfig()), ro)
        yield RunDeps(settings, rw, cp, AgentDeps(settings, gw, tools))
