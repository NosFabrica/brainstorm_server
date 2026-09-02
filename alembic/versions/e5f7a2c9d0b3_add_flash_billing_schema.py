"""add flash billing schema

Every billing migration this branch ever had, as one revision. Nothing outside
the branch has run any of them, so the intermediate states have no value — and
several of them existed only to undo each other: columns created for a later
revision to drop, and a `subscription_tier` that was created and then removed.
None of that is created here in the first place.

`billing_plan` is a mapping table from the start — which Flash plan grants
which policy, and whether we sell it. Price, period, ordering and copy are
Flash's answer, read from `GET /services/{id}`, never transcribed here.

Revision ID: e5f7a2c9d0b3
Revises: d4e5f6a7b8c9
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e5f7a2c9d0b3'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'flash_webhook_event',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event', sa.String(), nullable=False),
        sa.Column('event_timestamp', sa.DateTime(), nullable=True),
        sa.Column('delivery_timestamp', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.String(), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('processing_started_at', sa.DateTime(), nullable=True),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('process_error', sa.String(), nullable=True),
        # Who resolved an unattributed signup by hand, and how. A grant made by
        # a person should be as traceable as one made by a webhook.
        sa.Column('resolved_by', sa.String(), nullable=True),
        sa.Column('resolution', sa.String(), nullable=True),
        sa.Column('dedupe_key', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedupe_key'),
    )
    op.create_index(
        op.f('ix_flash_webhook_event_subscription_id'),
        'flash_webhook_event', ['subscription_id'], unique=False,
    )

    op.create_table(
        'billing_plan',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('flash_service_id', sa.String(), nullable=False),
        sa.Column('flash_plan_id', sa.String(), nullable=False),
        sa.Column('scheduling_id', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['scheduling_id'], ['scheduling.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('flash_service_id', 'flash_plan_id', name='uq_billing_plan_flash_ids'),
    )

    op.create_table(
        'user_subscription',
        sa.Column('pubkey', sa.String(), nullable=False),
        sa.Column('flash_subscription_id', sa.String(), nullable=False),
        sa.Column('flash_subscriber_id', sa.String(), nullable=True),
        sa.Column('billing_plan_id', sa.Integer(), nullable=False),
        sa.Column('granted_scheduling_id', sa.Integer(), nullable=True),
        sa.Column('flash_status', sa.String(), nullable=False),
        sa.Column('current_period_start', sa.DateTime(), nullable=True),
        sa.Column('current_period_end', sa.DateTime(), nullable=True),
        sa.Column('next_billing_date', sa.DateTime(), nullable=True),
        sa.Column('trial_end_date', sa.DateTime(), nullable=True),
        sa.Column('cancel_effective_date', sa.DateTime(), nullable=True),
        sa.Column('rail', sa.String(), nullable=True),
        sa.Column('portal_url', sa.String(), nullable=True),
        sa.Column('last_event_at', sa.DateTime(), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('last_sync_error', sa.String(), nullable=True),
        # Sticky clock: only a *change* of reason restarts it.
        sa.Column('sync_error_since', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['billing_plan_id'], ['billing_plan.id']),
        sa.ForeignKeyConstraint(['granted_scheduling_id'], ['scheduling.id']),
        sa.PrimaryKeyConstraint('pubkey'),
    )
    op.create_index(
        op.f('ix_user_subscription_flash_subscription_id'),
        'user_subscription', ['flash_subscription_id'], unique=False,
    )

    op.add_column(
        'brainstorm_nsec',
        sa.Column('billing_blocked', sa.Boolean(), server_default='false', nullable=False),
    )
    op.add_column(
        'brainstorm_nsec',
        sa.Column('scheduling_source', sa.String(), server_default='default', nullable=False),
    )

    # A policy is what a subscriber receives; `is_public` is what makes one
    # sellable. Without the backfill a fresh install has no public policy and
    # the pricing page offers nothing at all.
    op.add_column(
        'scheduling',
        sa.Column('is_public', sa.Boolean(), server_default='false', nullable=False),
    )
    op.execute(
        """
        UPDATE scheduling SET is_public = true
        WHERE is_default
           OR id IN (SELECT scheduling_id FROM billing_plan WHERE is_active)
        """
    )


def downgrade() -> None:
    op.drop_column('scheduling', 'is_public')
    op.drop_column('brainstorm_nsec', 'scheduling_source')
    op.drop_column('brainstorm_nsec', 'billing_blocked')
    op.drop_index(
        op.f('ix_user_subscription_flash_subscription_id'), table_name='user_subscription'
    )
    op.drop_table('user_subscription')
    op.drop_table('billing_plan')
    op.drop_index(
        op.f('ix_flash_webhook_event_subscription_id'), table_name='flash_webhook_event'
    )
    op.drop_table('flash_webhook_event')
