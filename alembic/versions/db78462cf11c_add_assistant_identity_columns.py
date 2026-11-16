"""add assistant_pubkey and nip05_local_part to brainstorm_nsec

Revision ID: db78462cf11c
Revises: e7b3c9d1f2a8
Create Date: 2026-11-03 00:00:00.000000

The Assistant pubkey and its NIP-05 local-part, stored so the well-known lookup
is one indexed SELECT instead of decrypting every nsec. Existing rows are filled
at app startup (the Fernet key isn't available to migrations).
"""
from alembic import op
import sqlalchemy as sa


revision = 'db78462cf11c'
down_revision = 'e7b3c9d1f2a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('brainstorm_nsec', sa.Column('assistant_pubkey', sa.String(), nullable=True))
    op.add_column('brainstorm_nsec', sa.Column('nip05_local_part', sa.String(), nullable=True))
    op.create_index(
        'ix_brainstorm_nsec_nip05_local_part', 'brainstorm_nsec', ['nip05_local_part']
    )


def downgrade() -> None:
    op.drop_index('ix_brainstorm_nsec_nip05_local_part', table_name='brainstorm_nsec')
    op.drop_column('brainstorm_nsec', 'nip05_local_part')
    op.drop_column('brainstorm_nsec', 'assistant_pubkey')
