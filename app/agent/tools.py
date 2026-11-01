"""The tools the agent's model can call. All read-only: retrieval and the fleet views go through the
`tools_ro` role (DESIGN §5.2). `query_sdr` joins when SDR data is loaded (#6).

Each tool returns (text for the model, evidence for state). Output is capped at about 2k tokens
(DESIGN §7.1 context bounds), and a validation error is returned to the model as the tool result,
not raised (DESIGN §6.3).
"""

import json
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from app.agent.schemas import GetAircraftArgs, SearchDocumentsArgs
from app.llm import CallContext
from app.llm.schema import strict_schema
from app.retrieval.search import RetrievalConfig, Retriever

MAX_TOOL_CHARS = 8_000  # about 2k tokens at the measured 3.97 chars per token
PASSAGE_CHARS = 1_500


def tool_spec(name: str, model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": (model.__doc__ or "").strip(),
                                             "parameters": strict_schema(model), "strict": True}}  # fmt: skip


TOOL_SPECS = [tool_spec("search_documents", SearchDocumentsArgs), tool_spec("get_aircraft", GetAircraftArgs)]


@dataclass
class ToolResult:
    text: str
    evidence: dict[str, dict[str, Any]]
    error: bool = False


@dataclass
class Tools:
    retriever: Retriever
    ro_pool: AsyncConnectionPool  # tools_ro

    async def call(self, name: str, arguments: str, ctx: CallContext) -> ToolResult:
        try:
            if name == "search_documents":
                return await self.search_documents(SearchDocumentsArgs.model_validate_json(arguments), ctx)
            if name == "get_aircraft":
                return await self.get_aircraft(GetAircraftArgs.model_validate_json(arguments))
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return ToolResult(f"Invalid arguments for {name}: {msg}. Fix them and call again.", {}, error=True)
        return ToolResult(f"Unknown tool {name}. Available: search_documents, get_aircraft.", {}, error=True)

    async def search_documents(self, args: SearchDocumentsArgs, ctx: CallContext) -> ToolResult:
        result = await self.retriever.search(args.query, ctx, levels=("public",),
                                             config=RetrievalConfig(k=args.top_k))  # fmt: skip
        if not result.hits:
            return ToolResult("No passages found. Try other technical terms.", {})
        parts, evidence, used = [], {}, 0
        for h in result.hits:
            where = h.page_label or (f"PDF p. {h.page}" if h.page else "")
            ad = (
                f", AD {h.ad_number}" + (f" (superseded by {h.superseded_by})" if h.superseded_by else "")
                if h.ad_number
                else ""
            )
            text = " ".join(h.text.split())[:PASSAGE_CHARS]
            block = f"[{h.chunk_id}] {h.title}{', ' + where if where else ''}{ad}\n{text}"
            if used + len(block) > MAX_TOOL_CHARS:
                break
            used += len(block)
            parts.append(block)
            evidence[h.chunk_id] = {"doc_id": h.doc_id, "title": h.title, "source": h.source, "page": h.page,
                                    "page_label": h.page_label, "ad_number": h.ad_number,
                                    "superseded_by": h.superseded_by, "text": h.text}  # fmt: skip
        note = f"\n(retrieval degraded: {', '.join(result.degraded)})" if result.degraded else ""
        return ToolResult("\n\n".join(parts) + note, evidence)

    async def get_aircraft(self, args: GetAircraftArgs) -> ToolResult:
        async with self.ro_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT registration, type, engines, hours, cycles, in_service FROM tools_fleet "
                "WHERE registration = %s",
                (args.registration,),
            )
            row = await cur.fetchone()
            if row is None:
                # The model must not substitute another aircraft (the DeepSeek failure in SPIKE §1).
                return ToolResult(f"{args.registration} is not in the fleet. Do not use any other aircraft.", {})
            cur = await conn.execute(
                "SELECT title, priority, status, created_at::date FROM tools_work_orders WHERE registration = %s "
                "ORDER BY created_at DESC LIMIT 5",
                (args.registration,),
            )
            orders = await cur.fetchall()
        info = {"registration": row[0], "type": row[1], "engines": row[2], "hours": float(row[3]), "cycles": row[4],
                "in_service": row[5], "recent_work_orders": [{"title": o[0], "priority": o[1], "status": o[2],
                                                              "date": str(o[3])} for o in orders]}  # fmt: skip
        return ToolResult(json.dumps(info), {})
