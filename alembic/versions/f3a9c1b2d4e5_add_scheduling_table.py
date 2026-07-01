"""add scheduling table + brainstorm_nsec.scheduling_id

Revision ID: f3a9c1b2d4e5
Revises: c89a28713591
Create Date: 2026-06-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3a9c1b2d4e5'
down_revision = 'c89a28713591'
branch_labels = None
depends_on = None


def upgrade() -> None:
    scheduling = op.create_table(
        'scheduling',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('schedule_interval_seconds', sa.Integer(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    # Seed the single default policy: weekly auto-recalc, base priority.
    op.bulk_insert(
        scheduling,
        [
            {
                'name': 'Weekly',
                'schedule_interval_seconds': 604800,  # 7 days
                'priority': 0,
                'enabled': True,
                'is_default': True,
            }
        ],
    )
    # At most one default policy: partial unique index over the truthy rows.
    op.create_index(
        'uq_scheduling_single_default',
        'scheduling',
        ['is_default'],
        unique=True,
        postgresql_where=sa.text('is_default'),
    )
    # NULL = default policy; no backfill needed for existing users.
    op.add_column(
        'brainstorm_nsec',
        sa.Column(
            'scheduling_id',
            sa.Integer(),
            sa.ForeignKey('scheduling.id'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('brainstorm_nsec', 'scheduling_id')
    op.drop_index('uq_scheduling_single_default', table_name='scheduling')
    op.drop_table('scheduling')
