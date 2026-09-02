"""record Flash's portal link per subscription

The manage link a subscriber follows is Flash's answer about their own
subscription, not one we spell out of a base URL and a service id. It arrives
on every subscription read, so it is recorded as read — the alternative is
asking Flash again on every signed-in page view.

Nullable and unbackfilled: existing rows have no link until their next sync,
and the read side already answers "no link" rather than inventing one.

Revision ID: d3b6f0c81a24
Revises: c1d2e3f4a5b6
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd3b6f0c81a24'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_subscription', sa.Column('portal_url', sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('user_subscription', 'portal_url')
