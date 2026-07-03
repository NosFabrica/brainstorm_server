"""add observerwhitelist table

Revision ID: a1b2c3d4e5f6
Revises: e0a1b2c3d4f5
Create Date: 2026-07-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'e0a1b2c3d4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'observerwhitelist',
        sa.Column('observer_pubkey', sa.String(), primary_key=True),
        sa.Column(
            'scores',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            'last_request_id',
            sa.Integer(),
            sa.ForeignKey('brainstorm_request.private_id'),
            nullable=True,
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('observerwhitelist')
