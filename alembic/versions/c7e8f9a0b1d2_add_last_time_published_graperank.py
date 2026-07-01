"""add last_time_published_graperank to brainstorm_nsec

Revision ID: c7e8f9a0b1d2
Revises: b2d4e5f6a7c8
Create Date: 2026-07-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c7e8f9a0b1d2'
down_revision = 'b2d4e5f6a7c8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'brainstorm_nsec',
        sa.Column('last_time_published_graperank', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_nsec', 'last_time_published_graperank')
