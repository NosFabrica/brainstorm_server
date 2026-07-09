"""add publish_duration_seconds to brainstorm_request

Revision ID: e0a1b2c3d4f5
Revises: d9f0a1b2c3e4
Create Date: 2026-07-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e0a1b2c3d4f5'
down_revision = 'd9f0a1b2c3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'brainstorm_request',
        sa.Column('publish_duration_seconds', sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_request', 'publish_duration_seconds')
