"""add scorecard

Revision ID: 8e2f4a6c1d05
Revises: 3b8e6f2a9c47
Create Date: 2026-11-19 00:00:00.000000

"""
from alembic import op

revision = "8e2f4a6c1d05"
down_revision = "3b8e6f2a9c47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # influence is double precision: `real` moves a score across a cutoff it sat
    # on, and costs the same space here (alignment padding).
    # fillfactor 85: a run rewrites most of an Observer's rows, and the free
    # space lets those updates stay on the same page.
    op.execute(
        """
        CREATE TABLE scorecard (
            observer_id integer NOT NULL,
            observee_id integer NOT NULL,
            influence double precision,
            hops smallint,
            verified_followers integer,
            PRIMARY KEY (observer_id, observee_id)
        ) WITH (fillfactor = 85)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE scorecard")
