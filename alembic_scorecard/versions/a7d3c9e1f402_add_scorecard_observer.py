"""add scorecard_observer

Revision ID: a7d3c9e1f402
Revises: 8e2f4a6c1d05
Create Date: 2026-11-20 00:00:00.000000

"""
from alembic import op

revision = "a7d3c9e1f402"
down_revision = "8e2f4a6c1d05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # When an Observer's Scorecards last changed, written in the same
    # transaction as the change. Readers use it as a cheap version stamp.
    op.execute(
        """
        CREATE TABLE scorecard_observer (
            observer_id integer PRIMARY KEY,
            written_at timestamptz NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE scorecard_observer")
