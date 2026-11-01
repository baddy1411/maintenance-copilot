"""Read-only view of recent work orders, for the agent's get_aircraft tool (issue #10).

Only the columns a technician needs for context: no approver or creator identities.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE VIEW tools_work_orders AS
            SELECT registration, title, priority, status, created_at FROM work_orders;
        GRANT SELECT ON tools_work_orders TO tools_ro, mcp_rw;
    """)


def downgrade() -> None:
    raise NotImplementedError("down-migrations are not supported")
