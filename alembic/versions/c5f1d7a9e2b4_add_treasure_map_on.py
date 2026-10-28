"""add treasure_map_on to brainstorm_nsec

Revision ID: c5f1d7a9e2b4
Revises: b4e8d2a6f0c1
Create Date: 2026-10-27 00:00:00.000000

The customer's choice for their Assistant's Treasure Map (its own kind 10040):
list its live duties, or none. Off until the first Turn on.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5f1d7a9e2b4'
down_revision = 'b4e8d2a6f0c1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'brainstorm_nsec',
        sa.Column('treasure_map_on', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_nsec', 'treasure_map_on')
