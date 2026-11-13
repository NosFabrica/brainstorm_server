"""add the assistant_keys_v1 view for the Assistant runner

Revision ID: a7c3e9d1f2b4
Revises: db78462cf11c
Create Date: 2026-11-13 00:00:00.000000

NosFabrica/assistant-runner and the task services it wakes (the Observer) read
each Assistant's key here to decrypt and sign as it themselves; this server is
never called during a run. Their role is granted SELECT on this view alone (ops,
not a migration), and a view runs with its owner's rights, so they see these
four columns and nothing else of brainstorm_nsec.

`legacy_nsec` is the plaintext `nsec` only where there is no token to decrypt,
exactly when `_plaintext_nsec` falls back to it (`if encrypted_nsec:` reads an
empty string as none too). The columns are a contract: change them in a
`assistant_keys_v2` beside this one, never here.
"""

from alembic import op

revision = "a7c3e9d1f2b4"
down_revision = "db78462cf11c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW assistant_keys_v1 AS
        SELECT pubkey AS owner,
               assistant_pubkey AS assistant,
               encrypted_nsec,
               CASE WHEN encrypted_nsec IS NULL OR encrypted_nsec = ''
                    THEN nsec END AS legacy_nsec
        FROM brainstorm_nsec
        WHERE assistant_pubkey IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS assistant_keys_v1")
