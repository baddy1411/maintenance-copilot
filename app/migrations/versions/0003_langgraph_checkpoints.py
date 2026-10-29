"""LangGraph checkpoint tables (langgraph-checkpoint-postgres 3.2.0).

`PostgresSaver.setup()` is versioned (its own `checkpoint_migrations` table) and idempotent, but some of
its steps use CREATE INDEX CONCURRENTLY, which cannot run inside Alembic's transaction. So it runs on its own
autocommit connection. A later checkpointer bump adds a revision that calls setup() again.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from alembic import op
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")


def upgrade() -> None:
    url = op.get_bind().engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with Connection.connect(url, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        PostgresSaver(conn).setup()
    # Only app_rw (API and agent jobs) touches checkpoints; tools, ingestion and MCP have no access.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO app_rw")


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
