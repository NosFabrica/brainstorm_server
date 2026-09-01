"""add brainstorm_nsec.assistant_kind0_published_at

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a7
Create Date: 2026-08-24 00:00:00.000000

Null = the Assistant's kind-0 profile was never published. The TA upload task
publishes it best-effort before an observer's first TA batch and sets this; the
manual POST /user/assistantProfile sets it too. No backfill: existing Assistants
get one republish on their next run — kind 0 is replaceable, so it's harmless.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'brainstorm_nsec',
        sa.Column('assistant_kind0_published_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_nsec', 'assistant_kind0_published_at')
