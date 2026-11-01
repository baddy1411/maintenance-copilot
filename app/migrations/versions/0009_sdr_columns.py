"""Service Difficulty Reports: the columns the agent's query_sdr tool filters and counts on (issue #6, docs/sdr.md).

- `model_family` (737, CRJ, A320, E-Jet...): questions name families more often than exact models.
- `operator_code` (the airline's 4-letter FAA designator), `part_condition` (CORRODED, CRACKED...),
  `part_location`, `nature` (the nature-of-condition codes) and `submitted_on`.
- `source_year`: the yearly file a report came from. A re-sync tombstones that file's reports that have
  disappeared from it, and must not touch the other years.
`ingest_runs.tombstoned` records how many reports a sync withdrew, so a sudden spike can alert (#16).
The registration (N-number) and serial numbers are not stored: no question needs them (data minimisation).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-01
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sdr
            ADD COLUMN model_family   text,
            ADD COLUMN operator_code  text,
            ADD COLUMN part_condition text,
            ADD COLUMN part_location  text,
            ADD COLUMN nature         text[] NOT NULL DEFAULT '{}',
            ADD COLUMN submitted_on   date,
            ADD COLUMN source_year    int NOT NULL;
        ALTER TABLE ingest_runs ADD COLUMN tombstoned int NOT NULL DEFAULT 0;
        CREATE INDEX sdr_family_jasc_date ON sdr (model_family, jasc, difficulty_date);
        CREATE INDEX sdr_source_year ON sdr (source_year);
        CREATE INDEX sdr_date ON sdr (difficulty_date);

        CREATE OR REPLACE VIEW tools_sdr AS
            SELECT sdr_id, difficulty_date, jasc, make, model, part_name, part_number, stage, how_discovered,
                   discrepancy, tsv, model_family, operator_code, part_condition, part_location, nature
            FROM sdr WHERE tombstoned_at IS NULL;
        GRANT SELECT ON tools_sdr TO tools_ro, mcp_rw;
    """)


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
