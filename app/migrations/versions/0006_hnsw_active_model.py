"""Partial HNSW index for the initial active embedding model (voyageai/voyage-4-lite), issue #8.

A later embedder change builds its own index with the same helper (DESIGN §8.1). CREATE INDEX CONCURRENTLY
can't run in a transaction, so this runs in Alembic's autocommit block. `maintenance_work_mem` is raised
for the build so the graph is built in memory (53k x 1024-d halfvec is about 110 MB of vectors).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30
"""

from alembic import op

from app.retrieval.vector_index import create_index_sql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

MODEL = "voyageai/voyage-4-lite"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET maintenance_work_mem = '1GB'")
        op.execute(create_index_sql(MODEL))
        op.execute("RESET maintenance_work_mem")


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
