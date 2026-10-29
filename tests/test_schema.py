"""Migrations and role boundaries on a real Postgres (DESIGN §5.1, §5.2). Needs Docker."""

import json
import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.config import Settings
from app.migrate import MIGRATIONS, ExtensionMismatch, check_vector_extension, migrate

pytestmark = pytest.mark.integration


def test_migrate_is_idempotent(db: Settings) -> None:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory(str(MIGRATIONS)).get_current_head()
    assert migrate(db) == head  # second run on an up-to-date database is a no-op


def test_pgvector_version_is_pinned(db: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = db.dsn("migrator", driver="postgresql+psycopg")
    assert check_vector_extension(dsn) == "0.8.2"
    monkeypatch.setattr("app.migrate.PGVECTOR_VERSION", "9.9.9")
    with pytest.raises(ExtensionMismatch):
        check_vector_extension(dsn)


def test_tools_ro_reads_views_only(connect: Any) -> None:
    conn = connect("tools_ro")
    conn.execute("SELECT count(*) FROM tools_chunks")
    conn.execute("SELECT active_embed_model FROM tools_retrieval_config")
    for table in ("chunks", "documents", "runs", "work_orders", "audit_log", "checkpoints"):
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")


def test_tools_ro_session_is_read_only_and_bounded(connect: Any) -> None:
    conn = connect("tools_ro")
    settings = {
        name: conn.execute(f"SHOW {name}").fetchone()[0]
        for name in (
            "default_transaction_read_only",
            "statement_timeout",
            "hnsw.ef_search",
            "hnsw.iterative_scan",
        )
    }
    assert settings == {
        "default_transaction_read_only": "on",
        "statement_timeout": "5s",
        "hnsw.ef_search": "100",
        "hnsw.iterative_scan": "relaxed_order",
    }
    with pytest.raises(errors.QueryCanceled):
        conn.execute("SELECT pg_sleep(6)")


def test_service_roles_cannot_run_ddl(connect: Any) -> None:
    for role in ("app_rw", "tools_ro", "ingest_rw", "mcp_rw"):
        with pytest.raises((errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)):
            connect(role).execute("CREATE TABLE sneaky (id int)")


def test_audit_log_is_append_only(connect: Any) -> None:
    app = connect("app_rw")
    app.execute("INSERT INTO audit_log (actor, action, row_hash) VALUES ('u1', 'test', 'h')")
    for stmt in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(errors.InsufficientPrivilege):
            app.execute(stmt)
    with pytest.raises(errors.InsufficientPrivilege):
        connect("ingest_rw").execute("DELETE FROM audit_log")


def _seed_run(migrator: psycopg.Connection[Any], created_by: str = "tech1") -> uuid.UUID:
    migrator.execute(
        "INSERT INTO fleet (registration, type, engines) VALUES ('MX-001', 'B737-800', 'CFM56-7B') "
        "ON CONFLICT DO NOTHING"
    )
    row = migrator.execute(
        "INSERT INTO runs (created_by, registration, symptom, client_key, request_sha256, graph_version, "
        "prompt_version, config_version, state_schema_version) "
        "VALUES (%s, 'MX-001', 'oil leak', %s, 'r', 'g1', 'p1', 'c1', 1) RETURNING run_id",
        (created_by, str(uuid.uuid4())),
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def test_one_decision_and_one_work_order_per_run(connect: Any) -> None:
    run_id = _seed_run(connect("migrator"))
    app = connect("app_rw")
    approve = (
        "INSERT INTO approvals (run_id, approver_id, action, draft_sha256) "
        "VALUES (%s, 'sup1', 'approve', 'd') RETURNING approval_id"
    )
    row = app.execute(approve, (run_id,)).fetchone()
    with pytest.raises(errors.UniqueViolation):
        app.execute(approve, (run_id,))
    write = (
        "INSERT INTO work_orders (run_id, approval_id, registration, title, priority, diagnosis, steps, "
        "citations, created_by, approved_by) VALUES (%s, %s, 'MX-001', 't', 'high', '{}', '[]', '[]', "
        "'tech1', 'sup1')"
    )
    app.execute(write, (run_id, row[0]))
    with pytest.raises(errors.UniqueViolation):
        app.execute(write, (run_id, row[0]))


def test_database_rejects_self_approved_work_order(connect: Any) -> None:
    run_id = _seed_run(connect("migrator"), created_by="sup1")
    app = connect("app_rw")
    row = app.execute(
        "INSERT INTO approvals (run_id, approver_id, action, draft_sha256) "
        "VALUES (%s, 'sup1', 'approve', 'd') RETURNING approval_id",
        (run_id,),
    ).fetchone()
    with pytest.raises(errors.CheckViolation):
        app.execute(
            "INSERT INTO work_orders (run_id, approval_id, registration, title, priority, diagnosis, "
            "steps, citations, created_by, approved_by) VALUES (%s, %s, 'MX-001', 't', 'low', '{}', "
            "'[]', '[]', 'sup1', 'sup1')",
            (run_id, row[0]),
        )


def test_client_key_dedupes_per_user(connect: Any) -> None:
    migrator = connect("migrator")
    _seed_run(migrator)
    insert = (
        "INSERT INTO runs (created_by, registration, symptom, client_key, request_sha256, graph_version, "
        "prompt_version, config_version, state_schema_version) "
        "VALUES (%s, 'MX-001', 's', 'same-key', 'r', 'g', 'p', 'c', 1)"
    )
    migrator.execute(insert, ("tech1",))
    migrator.execute(insert, ("tech2",))  # same key, different user: allowed
    with pytest.raises(errors.UniqueViolation):
        migrator.execute(insert, ("tech1",))


def test_fleet_registration_format(connect: Any) -> None:
    with pytest.raises(errors.CheckViolation):
        connect("migrator").execute("INSERT INTO fleet (registration, type, engines) VALUES ('N12345', 'x', 'y')")


async def test_app_rw_can_enqueue_jobs(db: Settings) -> None:
    import procrastinate

    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=db.dsn("app_rw")))

    @app.task(name="probe")
    def probe() -> None: ...

    async with app.open_async():
        job_id = await probe.defer_async()
    assert job_id > 0


def test_app_rw_can_checkpoint_and_resume_an_interrupt(db: Settings) -> None:
    """The approval pattern from SPIKE §3, on the migrated tables, as app_rw, without calling setup()."""
    from typing import TypedDict

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    class State(TypedDict, total=False):
        decision: str

    def approve(state: State) -> State:
        return {"decision": json.dumps(interrupt("draft"))}

    builder = StateGraph(State)
    builder.add_node("approve", approve)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    with PostgresSaver.from_conn_string(db.dsn("app_rw")) as saver:
        graph = builder.compile(checkpointer=saver)
        graph.invoke({}, config)  # type: ignore[arg-type]
        assert graph.get_state(config).next == ("approve",)  # type: ignore[arg-type]
    with PostgresSaver.from_conn_string(db.dsn("app_rw")) as saver:  # a second process, same tables
        graph = builder.compile(checkpointer=saver)
        out = graph.invoke(Command(resume={"action": "approve"}), config)  # type: ignore[arg-type]
    assert json.loads(out["decision"]) == {"action": "approve"}


def test_tool_and_ingest_roles_cannot_touch_checkpoints(connect: Any) -> None:
    for role in ("tools_ro", "ingest_rw", "mcp_rw"):
        with pytest.raises(errors.InsufficientPrivilege):
            connect(role).execute("SELECT 1 FROM checkpoints LIMIT 1")
