"""procrastinate 3.10.0 job queue schema.

procrastinate ships `schema.sql` for a fresh install and loose files under `sql/migrations` for upgrades,
with no version table of its own. So every procrastinate bump in `uv.lock` gets its own Alembic revision
that applies the upgrade files between the two versions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from typing import cast

from alembic import op
from procrastinate.schema import SchemaManager
from psycopg import Connection

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GRANTS = """
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'procrastinate%' LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO app_rw, ingest_rw', r.tablename);
    END LOOP;
END $$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw, ingest_rw;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO app_rw, ingest_rw;
"""


def upgrade() -> None:
    # The raw psycopg connection takes the SQL as-is (no bind-parameter parsing of ':' or '%').
    raw = cast(Connection[object], op.get_bind().connection.driver_connection)
    raw.execute(SchemaManager.get_schema())
    op.execute(GRANTS)


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
