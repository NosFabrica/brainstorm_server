"""billing_plan is a mapping table

Price, currency, billing period, ordering and copy were transcribed by hand
while Flash had no way to read a plan back. `GET /services/{id}` returns plan
objects now, so every one of those columns is dropped outright: keeping them
would keep a second, unverifiable answer to a question Flash already answers.

Dropped rather than staged. This branch has never been to production — staging
is the only place it has ever run, and the only thing these columns hold is the
transcription being replaced. The branch's migrations are squashed into one
before production, so this intermediate state has no archaeological value.

What stays is what Flash cannot know: which plan grants which scheduling
policy, and whether we sell it.

Revision ID: c1d2e3f4a5b6
Revises: b7d41e9a3c58
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b7d41e9a3c58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in (
        'amount_minor',
        'currency',
        'billing_period_unit',
        'billing_period_count',
        'sort_order',
        'blurb',
        'includes',
        'excludes',
    ):
        op.drop_column('billing_plan', column)


def downgrade() -> None:
    # The shape comes back; the values do not, and there is nowhere to read
    # them from — they only ever existed here.
    op.add_column('billing_plan', sa.Column('amount_minor', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('billing_plan', sa.Column('currency', sa.String(), nullable=False, server_default='USD'))
    op.add_column('billing_plan', sa.Column('billing_period_unit', sa.String(), nullable=True))
    op.add_column('billing_plan', sa.Column('billing_period_count', sa.Integer(), nullable=True))
    op.add_column('billing_plan', sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('billing_plan', sa.Column('blurb', sa.String(), nullable=True))
    op.add_column('billing_plan', sa.Column('includes', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('billing_plan', sa.Column('excludes', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
