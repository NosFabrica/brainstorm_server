"""idle_in_transaction_session_timeout on the Scorecard database

Revision ID: 3b8e6f2a9c47
Revises: 5c0a1d9e7b31
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op

revision = "3b8e6f2a9c47"
down_revision = "5c0a1d9e7b31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Applies to new sessions. Needs the database's owner.
    op.execute(
        """
        DO $$ BEGIN
            EXECUTE format(
                'ALTER DATABASE %I SET idle_in_transaction_session_timeout = %L',
                current_database(), '5min'
            );
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            EXECUTE format(
                'ALTER DATABASE %I RESET idle_in_transaction_session_timeout',
                current_database()
            );
        END $$
        """
    )
