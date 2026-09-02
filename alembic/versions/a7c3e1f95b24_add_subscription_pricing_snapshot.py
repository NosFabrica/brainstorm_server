"""add subscription pricing snapshot

What a subscriber is charged, as Flash recorded it when they subscribed —
see `UserSubscription` for why it is stored rather than looked up.

Nullable, and null means unpriced rather than free: rows written before this
column carry nothing until their next sync.

Revision ID: a7c3e1f95b24
Revises: e5f7a2c9d0b3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7c3e1f95b24'
down_revision: Union[str, None] = 'e5f7a2c9d0b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_subscription',
        sa.Column('pricing_amount_minor', sa.Integer(), nullable=True),
    )
    op.add_column(
        'user_subscription', sa.Column('pricing_currency', sa.String(), nullable=True)
    )
    op.add_column(
        'user_subscription',
        sa.Column('pricing_billing_interval', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_subscription', 'pricing_billing_interval')
    op.drop_column('user_subscription', 'pricing_currency')
    op.drop_column('user_subscription', 'pricing_amount_minor')
