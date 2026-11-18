"""add compromised_since to compromised_pubkey

Revision ID: 8f61c2d4a7e3
Revises: 5d2b8e41c7a9
Create Date: 2026-11-16 23:30:00.000000

From when the key may have been in someone else's hands, when the admin knows
it (earlier than marking), so the UI can date the account's content without a
hard-coded list.
"""
import sqlalchemy as sa
from alembic import op

revision = "8f61c2d4a7e3"
down_revision = "5d2b8e41c7a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "compromised_pubkey",
        sa.Column("compromised_since", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("compromised_pubkey", "compromised_since")
