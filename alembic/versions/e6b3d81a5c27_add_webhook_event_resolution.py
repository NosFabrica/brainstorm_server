"""add flash_webhook_event.resolved_by and .resolution

Revision ID: e6b3d81a5c27
Revises: c5f1a72b9d40
Create Date: 2026-08-31 10:00:00.000000

A signup Flash sent us with no reference of ours can only be settled by hand,
either onto the person who made it or as not a customer at all. Both write
`processed_at`, which is indistinguishable from what the automatic path writes —
so these two columns say a human decided it, who, and which way.

Null for every existing row and for everything the automatic path settles.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e6b3d81a5c27'
down_revision = 'c5f1a72b9d40'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'flash_webhook_event',
        sa.Column('resolved_by', sa.String(), nullable=True),
    )
    op.add_column(
        'flash_webhook_event',
        sa.Column('resolution', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('flash_webhook_event', 'resolution')
    op.drop_column('flash_webhook_event', 'resolved_by')
