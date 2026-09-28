"""add short_url

Revision ID: c3d4e5f6a7b8
Revises: c1d2e3f4a5b6
Create Date: 2026-08-21 00:00:00.000000

Additive; no data migrated (no codes were minted in a deployed env).
See docs/adr/0002-short-links-in-postgres.md.
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
        sa.UniqueConstraint("short_code"),
        sa.UniqueConstraint(
            "pubkey", "relays_fingerprint", name="uq_short_url_pubkey_fingerprint"
        ),
    )


def downgrade() -> None:
    op.drop_table("short_url")
