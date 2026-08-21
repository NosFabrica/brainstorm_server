"""add short_url — share links as a record of truth, not a cache

Revision ID: c3d4e5f6a7b8
Revises: f1a4c8e27b60
Create Date: 2026-08-21 00:00:00.000000

Short codes previously lived only in Redis, which runs allkeys-lru. An evicted
code 404s a public URL permanently and cannot be recomputed, so the record
belongs in Postgres. Additive and reversible; no data is migrated because no
codes were ever minted in a deployed environment.

See .scratch/shorturl/PRD.md D1.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3d4e5f6a7b8"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "short_url",
        sa.Column("id", sa.Integer(), nullable=False),
        # Variable-length on purpose: the generated length may change later and
        # already-shared codes must keep resolving.
        sa.Column("short_code", sa.String(length=32), nullable=False),
        sa.Column("pubkey", sa.String(length=64), nullable=False),
        sa.Column("relays_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "relays",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        # Also supplies the lookup index — no separate one needed.
        sa.UniqueConstraint("short_code"),
        # Makes minting idempotent per (pubkey, relay-set): a concurrent double
        # mint loses the race here rather than creating a second code.
        sa.UniqueConstraint(
            "pubkey", "relays_fingerprint", name="uq_short_url_pubkey_fingerprint"
        ),
    )


def downgrade() -> None:
    op.drop_table("short_url")
