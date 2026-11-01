"""The troubleshooting graph (DESIGN §6.3, docs/AGENT.md, docs/agent-runs.md):

    act ⇄ tools → diagnose → draft → approval (interrupt) → write | reject → END

`act` both plans and picks tools. AGENT.md had a separate `plan` LLM step, which would add a call and about
1-2 s without new information (docs/agent-runs.md §3.1). The step cap counts act → tools cycles.

State is plain JSON (dicts, lists, strings). Checkpoints are then safe to deserialize (no pickle, no
arbitrary types) and survive deploys as long as fields are only added (DESIGN §6.3.1). Models from
app/agent/schemas.py validate at each node. Every node returns an `event` that the runner records as a
run_event for the client.

After the interrupt, the decision only chooses the branch. `write` and `reject` re-read the `approvals` row
and check it against the run and this checkpoint's draft hash, in the same transaction as their writes
(app/agent/approval.py). A forged resume value can't produce a work order (tests/test_approval_property.py).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from psycopg_pool import AsyncConnectionPool

from app.agent.approval import reject_run, write_work_order
from app.agent.schemas import PRIORITY_ORDER, Citation, Diagnosis, WorkOrderDraft
from app.agent.tools import TOOL_SPECS, Tools
from app.config import Settings
from app.llm import CallContext, Gateway
from app.qa.answer import normalise

GRAPH_VERSION = "2"  # bump on any change to nodes or edges (DESIGN §6.3.1)
# Versions this code can resume. v1 ended at approval -> END; v2 adds write/reject *after* the interrupt, so a
# v1 run paused at `approval` resumes correctly here: nodes up to the interrupt are unchanged (DESIGN P2-33).
SERVED_GRAPH_VERSIONS = frozenset({"1", "2"})
STATE_SCHEMA_VERSION = 1  # bump on any non-additive change to AgentState
PROMPTS = {n: (Path(__file__).resolve().parent.parent / "prompts" / f"agent_{n}.md").read_text()
           for n in ("act", "diagnose", "draft")}  # fmt: skip
PROMPT_VERSION = hashlib.sha256("".join(PROMPTS[n] for n in sorted(PROMPTS)).encode()).hexdigest()[:12]
KEEP_FULL_TOOL_RESULTS = 4  # older tool results are reduced to the passage ids they returned
DIAGNOSIS_FOR_DRAFT = ("likely_causes", "confidence", "reasoning", "safety_notes", "evidence_incomplete")
CHUNK_ID = re.compile(r"^\[([^\]\s]+)\]", re.MULTILINE)


class AgentState(TypedDict, total=False):
    run_id: str
    registration: str
    symptom: str
    messages: list[dict[str, Any]]  # the act/tools conversation, without the system prompt
    evidence: dict[str, dict[str, Any]]  # chunk_id -> passage, from search_documents
    steps: int  # completed act -> tools cycles
    tool_errors: dict[str, int]
    evidence_incomplete: bool
    diagnosis: dict[str, Any] | None
    draft: dict[str, Any] | None
    draft_sha256: str | None
    decision: dict[str, Any] | None
    work_order_id: str | None
    event: dict[str, Any]  # what the last node did, for run_events


@dataclass
class AgentDeps:
    settings: Settings
    gateway: Gateway
    tools: Tools
    rw: AsyncConnectionPool  # app_rw: the write and reject nodes


def draft_sha256(draft: dict[str, Any]) -> str:
    """The supervisor's decision is bound to this hash (DESIGN §6.3), so it must be canonical."""
    return hashlib.sha256(json.dumps(draft, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compact(messages: list[dict[str, Any]], keep: int = KEEP_FULL_TOOL_RESULTS) -> list[dict[str, Any]]:
    """Context bound (DESIGN §7.1): tool results older than the last `keep` become one line listing the
    passage ids they returned. The full passages stay in `evidence` for diagnose and draft."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = set(tool_idx[:-keep]) if len(tool_idx) > keep else set()
    out = []
    for i, m in enumerate(messages):
        if i in old:
            ids = CHUNK_ID.findall(m.get("content") or "")
            m = {**m, "content": f"(earlier result, condensed: passages {', '.join(ids) or 'none'})"}
        out.append(m)
    return out


def check_citations(
    citations: list[Citation], evidence: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep citations that name a retrieved passage and quote it word for word (normalised)."""
    ok, dropped = [], []
    for c in citations:
        passage = evidence.get(c.chunk_id)
        if passage is None:
            dropped.append(f"{c.chunk_id}: not a retrieved passage")
        elif normalise(c.quote) not in normalise(passage["text"]):
            dropped.append(f"{c.chunk_id}: quote not found in passage")
        else:
            ok.append({"chunk_id": c.chunk_id, "quote": c.quote, "doc_id": passage["doc_id"], "title": passage["title"],
                       "page": passage["page"], "page_label": passage["page_label"],
                       "ad_number": passage["ad_number"], "superseded_by": passage["superseded_by"]})  # fmt: skip
    return ok, dropped


def evidence_block(evidence: dict[str, dict[str, Any]]) -> str:
    parts = []
    for cid, p in evidence.items():
        where = p["page_label"] or (f"PDF p. {p['page']}" if p["page"] else "")
        ad = (
            f", AD {p['ad_number']}" + (f", superseded by {p['superseded_by']}" if p["superseded_by"] else "")
            if p["ad_number"]
            else ""
        )
        text = " ".join(p["text"].split()).replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f'<passage id="{cid}" title="{p["title"]}" where="{where}{ad}">\n{text}\n</passage>')
    return "\n\n".join(parts) or "(no evidence was found)"


def build_graph(deps: AgentDeps, checkpointer: Any) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    s, gw, tools = deps.settings, deps.gateway, deps.tools

    def ctx(state: AgentState, kind: str) -> CallContext:
        from uuid import UUID

        return CallContext(kind=kind, run_id=UUID(state["run_id"]), prompt_version=PROMPT_VERSION)

    def opening(state: AgentState) -> dict[str, str]:
        return {"role": "user", "content": f"Aircraft: {state['registration']}\nReported symptom: {state['symptom']}"}

    async def act(state: AgentState) -> AgentState:
        msgs = [{"role": "system", "content": PROMPTS["act"]}, opening(state), *compact(state.get("messages", []))]
        r = await gw.chat(ctx(state, "agent_act"), msgs, max_tokens=s.agent_max_tokens, tools=TOOL_SPECS,
                          tool_choice="auto", reasoning={"effort": s.agent_reasoning_effort})  # fmt: skip
        calls = [{"name": t.name, "arguments": t.arguments} for t in r.tool_calls]
        return {"messages": [*state.get("messages", []), r.assistant_message()],
                "event": {"kind": "act", "text": r.text[:500], "tool_calls": calls}}  # fmt: skip

    async def run_tools(state: AgentState) -> AgentState:
        last = state["messages"][-1]
        messages, evidence = list(state["messages"]), dict(state.get("evidence", {}))
        errors, results = dict(state.get("tool_errors", {})), []
        for call in last.get("tool_calls", []):
            name, args = call["function"]["name"], call["function"]["arguments"]
            if errors.get(name, 0) >= 2:  # DESIGN §6.3: after 2 invalid calls, stop that branch
                out_text, ok, n = (
                    f"{name} is unavailable for the rest of this run after repeated invalid arguments.",
                    False,
                    0,
                )
            else:
                res = await tools.call(name, args, ctx(state, "agent_tool"))
                if res.error:
                    errors[name] = errors.get(name, 0) + 1
                evidence.update(res.evidence)
                out_text, ok, n = res.text, not res.error, len(res.evidence)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out_text})
            results.append({"name": name, "ok": ok, "passages": n})
        return {"messages": messages, "evidence": evidence, "tool_errors": errors, "steps": state.get("steps", 0) + 1,
                "event": {"kind": "tools", "results": results}}  # fmt: skip

    def after_act(state: AgentState) -> Literal["tools", "diagnose"]:
        wants_tools = bool(state["messages"][-1].get("tool_calls"))
        return "tools" if wants_tools and state.get("steps", 0) < s.agent_max_steps else "diagnose"

    async def diagnose(state: AgentState) -> AgentState:
        capped = bool(state["messages"][-1].get("tool_calls")) and state.get("steps", 0) >= s.agent_max_steps
        msgs = [{"role": "system", "content": PROMPTS["diagnose"]},
                {"role": "user", "content": f"Aircraft: {state['registration']}\nReported symptom: {state['symptom']}\n"
                 f"{'The step limit was reached before the evidence search finished.' if capped else ''}\n\n"
                 f"<evidence>\n{evidence_block(state.get('evidence', {}))}\n</evidence>"}]  # fmt: skip
        d = await gw.structured(ctx(state, "agent_diagnose"), msgs, Diagnosis, max_tokens=s.agent_structured_max_tokens,
                                reasoning_effort=s.agent_reasoning_effort)  # fmt: skip
        cites, dropped = check_citations(d.citations, state.get("evidence", {}))
        incomplete = d.evidence_incomplete or capped or not cites
        diag = {**d.model_dump(), "citations": cites, "evidence_incomplete": incomplete, "dropped_citations": dropped}
        return {"diagnosis": diag, "evidence_incomplete": incomplete, "event": {"kind": "diagnosis", "diagnosis": diag}}

    async def draft(state: AgentState) -> AgentState:
        diag = state["diagnosis"] or {}
        summary = {k: diag.get(k) for k in DIAGNOSIS_FOR_DRAFT}
        msgs = [{"role": "system", "content": PROMPTS["draft"]},
                {"role": "user", "content": f"Aircraft: {state['registration']}\nReported symptom: {state['symptom']}\n"
                 f"Diagnosis: {json.dumps(summary)}\n\n"
                 f"<evidence>\n{evidence_block(state.get('evidence', {}))}\n</evidence>"}]  # fmt: skip
        w = await gw.structured(ctx(state, "agent_draft"), msgs, WorkOrderDraft,
                                max_tokens=s.agent_structured_max_tokens,
                                reasoning_effort=s.agent_reasoning_effort)  # fmt: skip
        cites, dropped = check_citations(w.citations, state.get("evidence", {}))
        checks = list(dropped)
        doc = w.model_dump()
        if doc["registration"] != state["registration"]:  # the model must never switch aircraft (SPIKE §1)
            checks.append(f"registration corrected from {doc['registration']} to {state['registration']}")
            doc["registration"] = state["registration"]
        floor_ads = [c["ad_number"] for c in cites if c["ad_number"] and "before further flight"
                     in normalise(state["evidence"][c["chunk_id"]]["text"])]  # fmt: skip
        if floor_ads and PRIORITY_ORDER.index(doc["priority"]) < PRIORITY_ORDER.index("urgent"):
            checks.append(f"priority raised from {doc['priority']} to urgent: AD {', '.join(floor_ads)} requires "
                          "action before further flight")  # fmt: skip
            doc["priority"] = "urgent"
        doc.update(citations=cites, checks=checks, evidence_incomplete=state.get("evidence_incomplete", False))
        sha = draft_sha256(doc)
        return {"draft": doc, "draft_sha256": sha, "event": {"kind": "draft", "draft": doc, "draft_sha256": sha}}

    def approval(state: AgentState) -> AgentState:
        decision = interrupt(
            {"type": "approve_work_order", "draft": state["draft"], "draft_sha256": state["draft_sha256"]}
        )
        return {"decision": decision, "event": {"kind": "decision", "decision": decision}}

    def after_approval(state: AgentState) -> Literal["write", "reject"]:
        # Routing only. Both nodes re-check the approvals row, so a wrong or forged action here can't
        # cause a write (approval.py raises ApprovalInvalid).
        return "reject" if (state.get("decision") or {}).get("action") == "reject" else "write"

    async def write(state: AgentState) -> AgentState:
        from uuid import UUID

        wo_id, new = await write_work_order(deps.rw, UUID(state["run_id"]), dict(state))
        return {"work_order_id": str(wo_id), "event": {"kind": "written", "work_order_id": str(wo_id), "new": new}}

    async def reject(state: AgentState) -> AgentState:
        from uuid import UUID

        await reject_run(deps.rw, UUID(state["run_id"]), dict(state))
        return {"event": {"kind": "rejected"}}

    g = StateGraph(AgentState)
    g.add_node("act", act)
    g.add_node("tools", run_tools)
    g.add_node("diagnose", diagnose)
    g.add_node("draft", draft)
    g.add_node("approval", approval)
    g.add_node("write", write)
    g.add_node("reject", reject)
    g.add_edge(START, "act")
    g.add_conditional_edges("act", after_act, ["tools", "diagnose"])
    g.add_edge("tools", "act")
    g.add_edge("diagnose", "draft")
    g.add_edge("draft", "approval")
    g.add_conditional_edges("approval", after_approval, ["write", "reject"])
    g.add_edge("write", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=checkpointer)
