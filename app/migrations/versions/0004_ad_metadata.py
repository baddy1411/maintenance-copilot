"""AD metadata found while building ingestion (issue #5).

- `ad_number` is not unique: corrections (C1-...) and republications (R1-...) are separate Federal Register
  documents with the same AD number. 45 AD numbers in 2020-2026 appear on more than one document.
- New columns: `doc_kind` (original / correction / revision), `jasc` codes, and the applicability text.

Expand-only: N-1 code doesn't read the new columns, and dropping a unique constraint can't break it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE documents DROP CONSTRAINT documents_ad_number_key;
        CREATE INDEX documents_ad_number ON documents (ad_number);
        CREATE INDEX documents_supersedes ON documents USING gin (supersedes);
        ALTER TABLE documents
            ADD COLUMN doc_kind text NOT NULL DEFAULT 'original'
                CHECK (doc_kind IN ('original', 'correction', 'revision')),
            ADD COLUMN jasc text[] NOT NULL DEFAULT '{}',
            ADD COLUMN applicability text;

        CREATE OR REPLACE VIEW tools_chunks AS
            SELECT c.chunk_id, c.doc_id, c.page, c.ord, c.text, c.tsv,
                   d.source, d.title, d.ad_number, d.publication_date, d.effective_date,
                   d.superseded_by, d.make, d.models, d.url, d.access_level, d.doc_kind, d.jasc
            FROM chunks c JOIN documents d USING (doc_id);
    """)


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
