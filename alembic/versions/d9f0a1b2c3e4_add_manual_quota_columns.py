"""add manual quota columns to scheduling

Revision ID: d9f0a1b2c3e4
Revises: c7e8f9a0b1d2
Create Date: 2026-07-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd9f0a1b2c3e4'
down_revision = 'c7e8f9a0b1d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'scheduling',
        sa.Column('manual_quota_limit', sa.Integer(), nullable=False, server_default='20'),
    )
    op.add_column(
        'scheduling',
        sa.Column('manual_quota_window_seconds', sa.Integer(), nullable=False, server_default='604800'),
    )


def downgrade() -> None:
    op.drop_column('scheduling', 'manual_quota_window_seconds')
    op.drop_column('scheduling', 'manual_quota_limit')
