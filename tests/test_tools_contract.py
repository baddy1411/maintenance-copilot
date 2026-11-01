"""The tool layer's contract (issue #11, DESIGN §4, ADR-0008): the model's tools only ever get a `tools_ro`
connection, every tool's schema is in strict form, and unknown or malformed calls go back to the model."""

import ast
import json
from pathlib import Path
from typing import Any, cast

import pytest

from app.agent.tools import TOOL_NAMES, TOOL_SPECS, Tools
from app.config import Settings
from app.llm import CallContext, Gateway
from app.resources import agent_resources
from app.retrieval.search import Retriever

TOOLS_SRC = Path(__file__).parent.parent / "app" / "agent" / "tools.py"


def test_the_tool_module_cannot_open_connections_or_name_another_role() -> None:
    """Static: tools.py gets its pool injected, so it must not build DSNs, connect, or import the wiring."""
    src = TOOLS_SRC.read_text()
    for role in ("app_rw", "ingest_rw", "mcp_rw", "migrator"):
        assert role not in src
    tree = ast.parse(src)
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imports & {"app.resources", "app.config", "psycopg"}
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"dsn", "connect"}


@pytest.mark.integration
async def test_the_worker_wires_tools_with_the_tools_ro_role(db: Settings) -> None:
    async with agent_resources(db, gateway=cast(Gateway, object())) as deps:
        tools = deps.agent.tools
        for pool in (tools.ro_pool, tools.retriever.pool):
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT current_user, current_setting('default_transaction_read_only')")
                assert await cur.fetchone() == ("tools_ro", "on")


def test_every_tool_spec_is_strict_and_closed() -> None:
    assert TOOL_NAMES == ["search_documents", "get_aircraft", "query_sdr"]
    for spec in TOOL_SPECS:
        fn = spec["function"]
        params: dict[str, Any] = fn["parameters"]
        assert fn["strict"] is True and fn["description"]
        assert params["additionalProperties"] is False
        assert sorted(params["required"]) == sorted(params["properties"])
        assert "default" not in json.dumps(params)


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("drop_tables", "{}", "Unknown tool drop_tables. Available: search_documents, get_aircraft, query_sdr."),
        ("get_aircraft", '{"registration": "N12345"}', "Invalid arguments for get_aircraft: registration"),
        ("query_sdr", "not json", "Invalid arguments for query_sdr"),
        ("search_documents", '{"query": "x"}', "Invalid arguments for search_documents: query"),
    ],
)
async def test_bad_calls_are_returned_to_the_model_not_raised(name: str, arguments: str, message: str) -> None:
    tools = Tools(retriever=cast(Retriever, None), ro_pool=cast(Any, None))  # never reached
    r = await tools.call(name, arguments, CallContext(kind="agent_tool"))
    assert r.error and r.text.startswith(message) and r.evidence == {}
