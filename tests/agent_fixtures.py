"""Shared seed data and a scripted model for agent tests. Used in-process (with the fake OpenRouter) and by
the two-process kill test (tests/agent_proc.py), which can't share an in-memory fake."""

import json
from typing import Any
from uuid import UUID, uuid4

import psycopg

from app.agent.graph import GRAPH_VERSION, PROMPT_VERSION, STATE_SCHEMA_VERSION
from app.config import Settings
from app.ingest.store import halfvec
from app.seed import seed_fleet

VOYAGE = "voyageai/voyage-4-lite"
CHUNKS = [  # chunk_id, doc_id, source, ad_number, page, label, text
    ("hb:c0", "faa-amt-powerplant", "handbook", None, 329, "8-2",
     "A chip detector is a magnetic plug in the oil system that collects ferrous metal particles from the oil. "
     "Heavy metal particles in the oil indicate a definite engine failure, and the engine must be removed."),
    ("ad:x:c0", "ad:2026-99999", "ad", "2026-99-01", None, None,
     "[AD 2026-99-01: Boeing 737-800] To address metal contamination of the engine oil system, inspect the "
     "magnetic chip detector and oil filter before further flight and replace the engine if particles exceed limits."),
]  # fmt: skip
DIAGNOSIS = {
    "likely_causes": ["Internal engine bearing or gear wear shedding ferrous metal"], "confidence": "medium",
    "reasoning": "Metal on the chip detector indicates internal wear; quantity and type decide removal.",
    "citations": [
        {"chunk_id": "hb:c0", "quote": "Heavy metal particles in the oil indicate a definite engine failure"},
    ],
    "evidence_incomplete": False,
    "safety_notes": ["Possibly relevant AD 2026-99-01: inspect the chip detector before further flight"],
}  # fmt: skip
DRAFT = {
    "registration": "MX-001", "title": "Inspect chip detector and oil filter on engine 1 of MX-001",
    "priority": "medium", "diagnosis_summary": "Metal particles found in drained oil; possible internal wear.",
    "steps": ["Remove and inspect the magnetic chip detector", "Inspect the oil filter for metal",
              "Determine whether particles are ferrous with a magnet"],
    "citations": [{"chunk_id": "ad:x:c0",
                   "quote": "inspect the magnetic chip detector and oil filter before further flight"},
                  {"chunk_id": "hb:c0", "quote": "collects ferrous metal particles from the oil"}],
}  # fmt: skip
SCRIPT_ACT = [  # what the model does on each act turn
    [("get_aircraft", {"registration": "MX-001"})],
    [("search_documents", {"query": "metal particles in engine oil chip detector", "top_k": 5})],
    [],  # enough evidence: no tool calls
]


def seed(db: Settings) -> None:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE documents, chunks, chunk_embeddings, runs, run_events, llm_calls CASCADE")
        conn.execute("UPDATE retrieval_config SET active_embed_model = %s", (VOYAGE,))
        for cid, doc_id, source, ad, page, label, text in CHUNKS:
            conn.execute("INSERT INTO documents (doc_id, source, ingest_origin, title, ad_number, sha256, "
                         "access_level) "
                         "VALUES (%s, %s, 'official', %s, %s, 'x', 'public') ON CONFLICT DO NOTHING",
                         (doc_id, source, doc_id, ad))  # fmt: skip
            conn.execute("INSERT INTO chunks (chunk_id, doc_id, page, page_label, ord, text) VALUES (%s,%s,%s,%s,0,%s)",
                         (cid, doc_id, page, label, text))  # fmt: skip
            conn.execute(
                "INSERT INTO chunk_embeddings VALUES (%s, %s, %s::halfvec)", (cid, VOYAGE, halfvec([1.0] * 1024))
            )
    seed_fleet(db)


def create_run(db: Settings, registration: str = "MX-001", symptom: str = "Shiny metal in drained oil",
               user: str = "tech-1") -> UUID:  # fmt: skip
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO runs (created_by, registration, symptom, client_key, request_sha256, graph_version, "
            "prompt_version, config_version, state_schema_version) "
            "VALUES (%s,%s,%s,%s,'r',%s,%s,'c',%s) RETURNING run_id",
            (user, registration, symptom, str(uuid4()), GRAPH_VERSION, PROMPT_VERSION, STATE_SCHEMA_VERSION),
        ).fetchone()  # fmt: skip
    assert row is not None
    return UUID(str(row[0]))


def events(db: Settings, run_id: UUID) -> list[tuple[str, dict[str, Any]]]:
    with psycopg.connect(db.dsn("migrator")) as conn:
        return [(k, p) for k, p in conn.execute(
            "SELECT kind, payload FROM run_events WHERE run_id = %s ORDER BY seq", (run_id,)).fetchall()]  # fmt: skip


def run_row(db: Settings, run_id: UUID) -> dict[str, Any]:
    with psycopg.connect(db.dsn("migrator")) as conn:
        r = conn.execute(
            "SELECT status, attempts, draft_sha256, cost FROM runs WHERE run_id = %s", (run_id,)
        ).fetchone()
    assert r is not None
    return {"status": r[0], "attempts": r[1], "draft_sha256": r[2], "cost": float(r[3])}


def as_json(obj: Any) -> str:
    return json.dumps(obj)
