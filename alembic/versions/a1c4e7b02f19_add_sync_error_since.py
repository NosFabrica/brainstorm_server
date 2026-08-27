"""add user_subscription.sync_error_since

Revision ID: a1c4e7b02f19
Revises: 34b023889f82
Create Date: 2026-08-27 14:00:00.000000

How long a subscriber's read from Flash has been failing, which no existing
column can answer: `last_synced_at` is stamped on every attempt and `updated_at`
on every write, so both move forward while the failure persists.

Backfilled to NULL, including for rows already carrying a `last_sync_error`.
Those start their clock at their next failed read rather than being treated as
having failed since forever — one extra cycle is cheaper than wrongly writing
off a subscriber on the deploy.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1c4e7b02f19'
down_revision = '34b023889f82'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'user_subscription',
        sa.Column('sync_error_since', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_subscription', 'sync_error_since')
