"""add trigger_source column to brainstorm_request

Revision ID: b2d4e5f6a7c8
Revises: f3a9c1b2d4e5
Create Date: 2026-07-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2d4e5f6a7c8'
down_revision = 'f3a9c1b2d4e5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'brainstorm_request',
        sa.Column(
            'trigger_source',
            sa.String(length=128),
            server_default='manual',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_request', 'trigger_source')
