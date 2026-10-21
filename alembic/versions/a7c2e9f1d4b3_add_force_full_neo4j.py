"""add force_full_neo4j to brainstorm_request

Revision ID: a7c2e9f1d4b3
Revises: b4e8d2a6f0c1
Create Date: 2026-10-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "a7c2e9f1d4b3"
down_revision = "b4e8d2a6f0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "brainstorm_request",
        sa.Column(
            "force_full_neo4j", sa.Boolean(), server_default="false", nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("brainstorm_request", "force_full_neo4j")
