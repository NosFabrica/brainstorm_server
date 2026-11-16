"""add compromised_pubkey

Revision ID: c4e1a7d93b52
Revises: db78462cf11c
Create Date: 2026-11-16 00:00:00.000000

Accounts whose key is compromised: every Observer asserts them at rank 0, and no
GrapeRank run stores, publishes or whitelists a score for them. Admin-managed.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e1a7d93b52'
down_revision = 'db78462cf11c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'compromised_pubkey',
        sa.Column('pubkey', sa.String(), nullable=False),
        sa.Column('proof_event_id', sa.String(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('added_by', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('pubkey'),
    )


def downgrade() -> None:
    op.drop_table('compromised_pubkey')
