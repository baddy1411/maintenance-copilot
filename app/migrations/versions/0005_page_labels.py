"""Printed page labels for PDF chunks (issue #7).

`chunks.page` stays the 1-based PDF page index: the eval labels and `#page=N` deep links use it.
`page_label` is the printed number a technician sees on the page ("8-6", "10-25"), and citations show
that. It comes from the PDF's own page labels where present, otherwise from the page footer text
(docs/ingestion.md).

Expand-only: a nullable column plus a view that gains a trailing column.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE chunks ADD COLUMN page_label text;

        CREATE OR REPLACE VIEW tools_chunks AS
            SELECT c.chunk_id, c.doc_id, c.page, c.ord, c.text, c.tsv,
                   d.source, d.title, d.ad_number, d.publication_date, d.effective_date,
                   d.superseded_by, d.make, d.models, d.url, d.access_level, d.doc_kind, d.jasc,
                   c.page_label
            FROM chunks c JOIN documents d USING (doc_id);
    """)


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
