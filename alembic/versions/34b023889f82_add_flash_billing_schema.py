"""add flash billing schema

Revision ID: 34b023889f82
Revises: b2c3d4e5f6a7
Create Date: 2026-08-25 11:18:26.819829

Additive. Everything the Flash payments feature needs, in one revision.

`flash_webhook_event` is an inbox, not a ledger: rows are written before the
delivery is acknowledged, because Flash gives up after a few retries and never
replays. `dedupe_key` is unique so a retry collides rather than double-applies,
and `payload` is nullable so the personal fields can be pruned later while the
row itself stays for dedupe and audit.

`user_subscription.flash_status` is a plain string on purpose. Flash's status
set is open, and an unrecognised value must land intact rather than be coerced —
stored raw it is a display bug, translated on write it is data loss.

`brainstorm_nsec.scheduling_source` records who last set the tier, so billing
never overwrites a hand-granted one.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '34b023889f82'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('flash_webhook_event',
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
    sa.Column('dedupe_key', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedupe_key')
    )
    op.create_index(op.f('ix_flash_webhook_event_subscription_id'), 'flash_webhook_event', ['subscription_id'], unique=False)
    op.create_table('billing_plan',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('flash_service_id', sa.String(), nullable=False),
    sa.Column('flash_plan_id', sa.String(), nullable=False),
    sa.Column('subscription_tier', sa.String(), nullable=False),
    sa.Column('scheduling_id', sa.Integer(), nullable=False),
    sa.Column('amount_minor', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['scheduling_id'], ['scheduling.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('flash_service_id', 'flash_plan_id', name='uq_billing_plan_flash_ids')
    )
    op.create_table('user_subscription',
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
    sa.Column('email', sa.String(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('last_sync_error', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['billing_plan_id'], ['billing_plan.id'], ),
    sa.ForeignKeyConstraint(['granted_scheduling_id'], ['scheduling.id'], ),
    sa.PrimaryKeyConstraint('pubkey')
    )
    op.create_index(op.f('ix_user_subscription_email'), 'user_subscription', ['email'], unique=False)
    op.create_index(op.f('ix_user_subscription_flash_subscription_id'), 'user_subscription', ['flash_subscription_id'], unique=False)
    op.add_column('brainstorm_nsec', sa.Column('billing_blocked', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('brainstorm_nsec', sa.Column('scheduling_source', sa.String(), server_default='default', nullable=False))


def downgrade() -> None:
    op.drop_column('brainstorm_nsec', 'scheduling_source')
    op.drop_column('brainstorm_nsec', 'billing_blocked')
    op.drop_index(op.f('ix_user_subscription_flash_subscription_id'), table_name='user_subscription')
    op.drop_index(op.f('ix_user_subscription_email'), table_name='user_subscription')
    op.drop_table('user_subscription')
    op.drop_table('billing_plan')
    op.drop_index(op.f('ix_flash_webhook_event_subscription_id'), table_name='flash_webhook_event')
    op.drop_table('flash_webhook_event')
