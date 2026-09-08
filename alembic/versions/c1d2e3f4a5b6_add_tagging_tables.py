"""add nostr_tag_element + nostr_user_tagging tables

Revision ID: c1d2e3f4a5b6
Revises: f1a4c8e27b60
Create Date: 2026-08-25 00:00:00.000000

The input set for Trusted Lists: kind-39999 tag elements and the taggings that
apply them. See engineering-team/decisions/trusted-lists/0001.

Replaceability is a write-time invariant, so both tables carry the event's own
`created_at` (not ingest time) and the tag element's natural key is its
addressable coordinate, not its event id.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
# Re-parented twice, for the same reason both times: a sibling cut from the
# same parent gives alembic two heads, and `alembic upgrade head` — which
# start.sh runs at boot — then fails outright rather than picking one.
# First onto d4e5f6a7b8c9 (assistant kind-0), then onto f1a4c8e27b60 (Flash
# billing), which was itself cut from d4e5f6a7b8c9. Billing deploys ahead of
# this branch, so it goes first; the tables are disjoint, so the order carries
# no meaning beyond the deploy sequence.
down_revision = 'f1a4c8e27b60'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'nostr_tag_element',
        sa.Column('event_id', sa.String(length=64), primary_key=True),
        sa.Column('author_pubkey', sa.String(length=64), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False, server_default=''),
        sa.Column('description', sa.String(), nullable=False, server_default=''),
        sa.Column('created_at_unix', sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            'author_pubkey', 'slug', name='uq_nostr_tag_element_coordinate'
        ),
    )
    op.create_table(
        'nostr_user_tagging',
        sa.Column('asserter_pubkey', sa.String(length=64), primary_key=True),
        sa.Column('d_tag', sa.String(), primary_key=True),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('target_pubkey', sa.String(length=64), nullable=False),
        sa.Column('tag_event_id', sa.String(length=64), nullable=False),
        sa.Column('polarity', sa.Float(), nullable=False, server_default='1'),
        sa.Column('created_at_unix', sa.Integer(), nullable=False),
    )
    op.create_index(
        'ix_nostr_user_tagging_tag_event_id', 'nostr_user_tagging', ['tag_event_id']
    )
    op.create_index(
        'ix_nostr_user_tagging_target', 'nostr_user_tagging', ['target_pubkey']
    )


def downgrade() -> None:
    op.drop_index('ix_nostr_user_tagging_target', table_name='nostr_user_tagging')
    op.drop_index('ix_nostr_user_tagging_tag_event_id', table_name='nostr_user_tagging')
    op.drop_table('nostr_user_tagging')
    op.drop_table('nostr_tag_element')
