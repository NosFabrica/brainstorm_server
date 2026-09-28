"""add support schema

Revision ID: e5a1c9d3b7f2
Revises: c3d4e5f6a7b8
Create Date: 2026-09-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'e5a1c9d3b7f2'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'scheduling',
        sa.Column('support_included', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_table(
        'support_ticket',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pubkey', sa.String(length=64), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='open'),
        sa.Column('notify_email', sa.String(length=320), nullable=True),
        sa.Column('diagnostics', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('last_message_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('last_message_author', sa.String(length=16), nullable=False, server_default='user'),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_support_ticket_pubkey_last_message_at',
        'support_ticket',
        ['pubkey', sa.text('last_message_at DESC')],
    )
    op.create_index(
        'ix_support_ticket_last_message_at',
        'support_ticket',
        [sa.text('last_message_at DESC')],
    )
    op.create_table(
        'support_message',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('author', sa.String(length=16), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('actor_pubkey', sa.String(length=64), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')
        ),
        sa.ForeignKeyConstraint(
            ['ticket_id'], ['support_ticket.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_support_message_ticket_id_id', 'support_message', ['ticket_id', 'id']
    )
    op.create_table(
        'support_event',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=32), nullable=False),
        sa.Column('actor', sa.String(length=16), nullable=False),
        sa.Column('actor_pubkey', sa.String(length=64), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(
            ['ticket_id'], ['support_ticket.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_support_event_ticket_id_id', 'support_event', ['ticket_id', 'id']
    )


def downgrade() -> None:
    op.drop_index('ix_support_event_ticket_id_id', table_name='support_event')
    op.drop_table('support_event')
    op.drop_index('ix_support_message_ticket_id_id', table_name='support_message')
    op.drop_table('support_message')
    op.drop_index('ix_support_ticket_last_message_at', table_name='support_ticket')
    op.drop_index('ix_support_ticket_pubkey_last_message_at', table_name='support_ticket')
    op.drop_table('support_ticket')
    op.drop_column('scheduling', 'support_included')
