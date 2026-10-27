"""passkey_vault: passkey-locked key copies for signing in on a new device

Revision ID: d8f2a1c4e6b9
Revises: b4e8d2a6f0c1
Create Date: 2026-10-27 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "d8f2a1c4e6b9"
down_revision = "b4e8d2a6f0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "passkey_vault",
        sa.Column("credential_id_hash", sa.String(length=64), nullable=False),
        sa.Column("pubkey", sa.String(length=64), nullable=False),
        sa.Column("credential_public_key", sa.Text(), nullable=False),
        sa.Column("alg", sa.Integer(), nullable=False),
        sa.Column("blob", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column(
            "sign_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("credential_id_hash"),
    )
    op.create_index(
        op.f("ix_passkey_vault_pubkey"), "passkey_vault", ["pubkey"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_passkey_vault_pubkey"), table_name="passkey_vault")
    op.drop_table("passkey_vault")
