"""Spike 3: a LangGraph human-approval interrupt gates the only write, and that write is idempotent.

No LLM is used here: the diagnosis is stubbed so the test only exercises the control flow.
Run: uv run python s3_interrupt.py
"""

import hashlib
import sqlite3
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

db = sqlite3.connect(":memory:")
db.execute("""CREATE TABLE work_orders (
    wo_id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
    asset_id TEXT, title TEXT, status TEXT, approved_by TEXT)""")


class State(TypedDict, total=False):
    asset_id: str
    symptom: str
    draft: dict
    decision: dict
    work_order_id: int | None


def draft_node(state: State) -> State:
    title = f"Inspect bearings on {state['asset_id']}"
    key = hashlib.sha256(f"{state['asset_id']}|{title.lower()}".encode()).hexdigest()
    return {"draft": {"asset_id": state["asset_id"], "title": title, "idempotency_key": key}}


def approval_node(state: State) -> State:
    decision = interrupt({"type": "approve_work_order", "draft": state["draft"]})
    return {"decision": decision}


def route(state: State) -> str:
    return "write" if state["decision"]["action"] in ("approve", "edit") else END


def write_node(state: State) -> State:
    d = state.get("decision", {}).get("edited_draft") or state["draft"]
    db.execute(
        "INSERT INTO work_orders (idempotency_key, asset_id, title, status, approved_by) "
        "VALUES (?, ?, ?, 'approved', ?) ON CONFLICT(idempotency_key) DO NOTHING",
        (d["idempotency_key"], d["asset_id"], d["title"], state["decision"]["approver_id"]),
    )
    row = db.execute("SELECT wo_id FROM work_orders WHERE idempotency_key = ?", (d["idempotency_key"],)).fetchone()
    return {"work_order_id": row[0]}


def build(saver):
    g = StateGraph(State)
    g.add_node("draft", draft_node)
    g.add_node("approval", approval_node)
    g.add_node("write", write_node)
    g.add_edge(START, "draft")
    g.add_edge("draft", "approval")
    g.add_conditional_edges("approval", route, ["write", END])
    g.add_edge("write", END)
    return g.compile(checkpointer=saver)


def count() -> int:
    return db.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]


saver = InMemorySaver()
graph = build(saver)
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{'  ' + detail if detail else ''}")


# 1. The run pauses at the interrupt, and nothing has been written yet.
cfg = {"configurable": {"thread_id": "t-approve"}}
out = graph.invoke({"asset_id": "P-101", "symptom": "bearing temperature rising"}, cfg)
check("pauses at approval interrupt", "__interrupt__" in out, f"payload={out['__interrupt__'][0].value['type']}")
check("no write before approval", count() == 0, f"rows={count()}")

# 2. Resume with approve -> exactly one row.
out = graph.invoke(Command(resume={"action": "approve", "approver_id": "sup_01"}), cfg)
check("approve writes one work order", count() == 1 and out["work_order_id"] == 1, f"rows={count()} wo_id={out.get('work_order_id')}")

# 3. Same request retried on a new thread (e.g. the client retried) -> still one row.
cfg2 = {"configurable": {"thread_id": "t-retry"}}
graph.invoke({"asset_id": "P-101", "symptom": "bearing temperature rising"}, cfg2)
out = graph.invoke(Command(resume={"action": "approve", "approver_id": "sup_01"}), cfg2)
check("retry is idempotent (no duplicate)", count() == 1 and out["work_order_id"] == 1, f"rows={count()}")

# 4. Reject -> no row, run ends.
cfg3 = {"configurable": {"thread_id": "t-reject"}}
graph.invoke({"asset_id": "C-202", "symptom": "short cycling"}, cfg3)
out = graph.invoke(Command(resume={"action": "reject", "approver_id": "sup_01", "reason": "needs more info"}), cfg3)
check("reject writes nothing", count() == 1 and out.get("work_order_id") is None, f"rows={count()}")

# 5. Resume from a freshly compiled graph that shares the checkpointer (simulates another API worker).
cfg4 = {"configurable": {"thread_id": "t-handoff"}}
graph.invoke({"asset_id": "C-203", "symptom": "high discharge temp"}, cfg4)
other_worker = build(saver)
pending = other_worker.get_state(cfg4)
check("other worker sees pending interrupt", bool(pending.interrupts), f"next={pending.next}")
out = other_worker.invoke(Command(resume={"action": "approve", "approver_id": "sup_02"}), cfg4)
check("other worker can resume and write", count() == 2, f"rows={count()}")

print(f"\n{sum(results)}/{len(results)} checks passed")
raise SystemExit(0 if all(results) else 1)
