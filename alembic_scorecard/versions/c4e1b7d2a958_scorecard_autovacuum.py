"""scorecard autovacuum scale factor

Revision ID: c4e1b7d2a958
Revises: a7d3c9e1f402
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op

revision = "c4e1b7d2a958"
down_revision = "a7d3c9e1f402"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Vacuum after 2% dead rows, not the default 20% of a very large table.
    op.execute("ALTER TABLE scorecard SET (autovacuum_vacuum_scale_factor = 0.02)")


def downgrade() -> None:
    op.execute("ALTER TABLE scorecard RESET (autovacuum_vacuum_scale_factor)")
