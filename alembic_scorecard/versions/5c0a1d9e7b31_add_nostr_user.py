"""add nostr_user

Revision ID: 5c0a1d9e7b31
Revises:
Create Date: 2026-11-16 00:00:00.000000

"""
from alembic import op

revision = "5c0a1d9e7b31"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE nostr_user_id_seq AS integer")
    op.execute(
        """
        CREATE TABLE nostr_user (
            id integer PRIMARY KEY DEFAULT nextval('nostr_user_id_seq'),
            pubkey text NOT NULL UNIQUE,
            CONSTRAINT nostr_user_pubkey_is_hex CHECK (pubkey ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute("ALTER SEQUENCE nostr_user_id_seq OWNED BY nostr_user.id")


def downgrade() -> None:
    op.execute("DROP TABLE nostr_user")
