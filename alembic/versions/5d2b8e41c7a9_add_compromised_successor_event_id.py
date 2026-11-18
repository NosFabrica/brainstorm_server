"""add successor_event_id to compromised_pubkey

Revision ID: 5d2b8e41c7a9
Revises: c4e1a7d93b52
Create Date: 2026-11-16 21:30:00.000000

The owner's new account's post about the move, so the UI can show it beside
the new account without a hard-coded list.
"""
import sqlalchemy as sa
from alembic import op

revision = "5d2b8e41c7a9"
down_revision = "c4e1a7d93b52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compromised_pubkey",
        sa.Column("successor_event_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("compromised_pubkey", "successor_event_id")
