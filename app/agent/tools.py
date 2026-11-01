"""The tools the agent's model can call. All read-only: retrieval, the fleet views and the Service
Difficulty Reports go through the `tools_ro` role (DESIGN §5.2). `query_sdr` is the structured SQL tool of
ADR-0008 (docs/sdr.md).

Each tool returns (text for the model, evidence for state). Output is capped at about 2k tokens
(DESIGN §7.1 context bounds), and a validation error is returned to the model as the tool result,
not raised (DESIGN §6.3).
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from app.agent.schemas import GetAircraftArgs, QuerySdrArgs, SearchDocumentsArgs
from app.ingest.sdr_models import normalise_query_model
from app.llm import CallContext
from app.llm.schema import strict_schema
from app.retrieval.search import RetrievalConfig, Retriever

MAX_TOOL_CHARS = 8_000  # about 2k tokens at the measured 3.97 chars per token
PASSAGE_CHARS = 1_500


def tool_spec(name: str, model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": (model.__doc__ or "").strip(),
                                             "parameters": strict_schema(model), "strict": True}}  # fmt: skip


TOOL_SPECS = [tool_spec("search_documents", SearchDocumentsArgs), tool_spec("get_aircraft", GetAircraftArgs),
              tool_spec("query_sdr", QuerySdrArgs)]  # fmt: skip
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SPECS]
SDR_TEXT_CHARS = 600
SDR_TIMEOUT = "5s"  # ADR-0008
SDR_GROUP = {"count_by_jasc": "jasc", "count_by_part": "upper(part_name)",
             "count_by_month": "to_char(difficulty_date, 'YYYY-MM')"}  # fmt: skip


def sdr_filters(args: QuerySdrArgs) -> tuple[str, dict[str, Any], list[str]]:
    """WHERE clause, its parameters, and a readable description. Only fixed SQL fragments with bound
    parameters: nothing the model writes is ever spliced into the query (ADR-0008)."""
    where: list[str] = []
    params: dict[str, Any] = {}
    said: list[str] = []
    if args.aircraft:
        params["aircraft"] = normalise_query_model(args.aircraft)
        where.append("(lower(model) = lower(%(aircraft)s) OR lower(model_family) = lower(%(aircraft)s))")
        said.append(f"aircraft {params['aircraft']!r}")
    if args.jasc:
        params["jasc"] = args.jasc + "%"
        where.append("jasc LIKE %(jasc)s")
        said.append(f"JASC {args.jasc}*")
    if args.text:
        params["text"] = args.text
        where.append("tsv @@ websearch_to_tsquery('english', %(text)s)")
        said.append(f"narrative matches {args.text!r}")
    if args.part:
        params["part"] = "%" + re.sub(r"([\\%_])", r"\\\1", args.part) + "%"
        where.append("part_name ILIKE %(part)s")
        said.append(f"part name contains {args.part!r}")
    if args.date_from:
        params["date_from"] = args.date_from
        where.append("difficulty_date >= %(date_from)s")
        said.append(f"from {args.date_from}")
    if args.date_to:
        params["date_to"] = args.date_to
        where.append("difficulty_date <= %(date_to)s")
        said.append(f"to {args.date_to}")
    return " AND ".join(where), params, said


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
            if name == "query_sdr":
                return await self.query_sdr(QuerySdrArgs.model_validate_json(arguments))
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
            return ToolResult(f"Invalid arguments for {name}: {msg}. Fix them and call again.", {}, error=True)
        return ToolResult(f"Unknown tool {name}. Available: {', '.join(TOOL_NAMES)}.", {}, error=True)

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

    async def query_sdr(self, args: QuerySdrArgs) -> ToolResult:
        where, params, said = sdr_filters(args)
        async with self.ro_pool.connection() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{SDR_TIMEOUT}'")
            # `where`, `group` and `order` are fixed fragments chosen in code; values are bound (sdr_filters).
            stats = f"SELECT count(*), min(difficulty_date), max(difficulty_date) FROM tools_sdr WHERE {where}"  # noqa: S608
            cur = await conn.execute(stats, params)
            total, first, last = await cur.fetchone() or (0, None, None)
            if args.mode == "list":
                cur = await conn.execute(
                    f"""SELECT sdr_id, difficulty_date, make, model, jasc, part_name, part_condition, stage, discrepancy
                        FROM tools_sdr WHERE {where} ORDER BY difficulty_date DESC, sdr_id LIMIT %(limit)s""",  # noqa: S608
                    {**params, "limit": args.limit},
                )
            else:
                group = SDR_GROUP[args.mode]
                order = "1" if args.mode == "count_by_month" else "2 DESC, 1"
                limit = 60 if args.mode == "count_by_month" else args.limit  # every month in range, in order
                sql = f"SELECT {group}, count(*) FROM tools_sdr WHERE {where} GROUP BY 1 ORDER BY {order}"  # noqa: S608
                cur = await conn.execute(sql + " LIMIT %(limit)s", {**params, "limit": limit})
            rows = await cur.fetchall()
        head = (f"Service Difficulty Reports where {', '.join(said)}: {total:,} reports"
                + (f" ({first} to {last})." if total else "."))  # fmt: skip
        if not total:
            hint = " Try the family (e.g. '737', 'A320', 'CRJ') or fewer filters." if args.aircraft else ""
            return ToolResult(head + hint, {})
        note = "\nCounts are what operators chose to report, not failure rates."
        if args.mode != "list":
            label = {"count_by_jasc": "JASC", "count_by_part": "part", "count_by_month": "month"}[args.mode]
            lines = [f"{label} {g or '(blank)'}: {n:,}" for g, n in rows]
            return ToolResult("\n".join([head, *lines]) + note, {})
        parts, evidence, used = [head], {}, len(head)
        for sdr_id, when, make, model, jasc, part, cond, stage, text in rows:
            cid = f"sdr:{sdr_id}"
            narrative = " ".join(text.split())
            block = (f"[{cid}] {when} · {make} {model} · JASC {jasc or '?'} · {part or 'part not given'}"
                     f"{f' ({cond})' if cond else ''}{f' · stage {stage}' if stage else ''}\n"
                     f"{narrative[:SDR_TEXT_CHARS]}")  # fmt: skip
            if used + len(block) > MAX_TOOL_CHARS:
                break
            used += len(block)
            parts.append(block)
            evidence[cid] = {"doc_id": cid, "title": f"SDR {sdr_id} ({make} {model}, {when})", "source": "sdr",
                             "page": None, "page_label": None, "ad_number": None, "superseded_by": None,
                             "text": narrative}  # fmt: skip
        return ToolResult("\n\n".join(parts) + note, evidence)
