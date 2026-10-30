"""Vacuum chunk_embeddings sooner, because dead entries degrade HNSW recall (issue #9).

Found: re-ingesting the 4 PDFs replaced 8,935 of 53,133 embedding rows. HNSW recall@50 against an exact scan
dropped from 0.996 to 0.988, and vector-only recall@5 on the eval set from 0.80 to 0.75. A manual VACUUM
restored both (0.991, 0.80). Autovacuum never ran, because the default scale factor of 0.2 waits for about
10.6k dead rows on this table. 0.02 (plus a 1,000-row base) triggers after roughly 2,000.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE chunk_embeddings SET (
            autovacuum_vacuum_scale_factor = 0.02,
            autovacuum_vacuum_threshold = 1000,
            autovacuum_analyze_scale_factor = 0.02
        )
    """)


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
