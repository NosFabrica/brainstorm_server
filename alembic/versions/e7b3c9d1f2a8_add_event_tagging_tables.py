"""add nostr_tagging_header, nostr_event_tagging, nostr_tagged_event tables

Revision ID: e7b3c9d1f2a8
Revises: c5f1d7a9e2b4
Create Date: 2026-11-03 00:00:00.000000

The input set for Trusted Lists of tagged content: tagging headers, the event
taggings that point at them, and the by-id lookup cache. See
engineering-team/decisions/trusted-lists/0002 (D6). Like story 1's tables,
replaceability is a write-time invariant on the event's own `created_at`.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'e7b3c9d1f2a8'
down_revision = 'c5f1d7a9e2b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'nostr_tagging_header',
        sa.Column('author_pubkey', sa.String(length=64), primary_key=True),
        sa.Column('d_tag', sa.String(), primary_key=True),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('tag_author_pubkey', sa.String(length=64), nullable=True),
        sa.Column('tag_slug', sa.String(), nullable=True),
        sa.Column('tag_event_id', sa.String(length=64), nullable=True),
        sa.Column('created_at_unix', sa.Integer(), nullable=False),
    )
    op.create_table(
        'nostr_event_tagging',
        sa.Column('asserter_pubkey', sa.String(length=64), primary_key=True),
        sa.Column('d_tag', sa.String(), primary_key=True),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('target', sa.String(), nullable=False),
        sa.Column('target_slot', sa.String(length=1), nullable=False),
        sa.Column('target_relay', sa.String(), nullable=True),
        sa.Column('header_coordinate', sa.String(), nullable=False),
        sa.Column('polarity', sa.Float(), nullable=False, server_default='1'),
        sa.Column('created_at_unix', sa.Integer(), nullable=False),
    )
    op.create_index(
        'ix_nostr_event_tagging_header', 'nostr_event_tagging', ['header_coordinate']
    )
    op.create_table(
        'nostr_tagged_event',
        sa.Column('event_id', sa.String(length=64), primary_key=True),
        sa.Column('kind', sa.Integer(), nullable=False),
        sa.Column('pubkey', sa.String(length=64), nullable=False),
        sa.Column('d_tag', sa.String(), nullable=True),
        sa.Column(
            'z_values',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'names',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_table('nostr_tagged_event')
    op.drop_index('ix_nostr_event_tagging_header', table_name='nostr_event_tagging')
    op.drop_table('nostr_event_tagging')
    op.drop_table('nostr_tagging_header')
