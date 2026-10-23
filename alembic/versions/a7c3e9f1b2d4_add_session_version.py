"""add session_version to brainstorm_nsec

Revision ID: a7c3e9f1b2d4
Revises: e5a1c9d3b7f2
Create Date: 2026-10-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a7c3e9f1b2d4'
down_revision = 'e5a1c9d3b7f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'brainstorm_nsec',
        sa.Column('session_version', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_nsec', 'session_version')
