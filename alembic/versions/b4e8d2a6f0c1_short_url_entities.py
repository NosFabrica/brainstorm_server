"""short_url: a code points at an entity (kind, entity_key)

Revision ID: b4e8d2a6f0c1
Revises: a7c3e9f1b2d4
Create Date: 2026-10-22 00:00:00.000000

Existing rows become profiles (entity_key = pubkey), relays and codes kept.
Duplicate pubkeys (staging only) keep the oldest row.
"""

import sqlalchemy as sa
from alembic import op

revision = "b4e8d2a6f0c1"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("short_url", sa.Column("kind", sa.String(length=16), nullable=True))
    op.add_column(
        "short_url", sa.Column("entity_key", sa.String(length=512), nullable=True)
    )
    op.execute(
        "UPDATE short_url SET kind = 'profile', entity_key = pubkey WHERE kind IS NULL"
    )
    op.execute(
        """
        DELETE FROM short_url s USING short_url o
        WHERE s.kind = o.kind AND s.entity_key = o.entity_key
          AND (o.created_at, o.id) < (s.created_at, s.id)
        """
    )
    op.alter_column("short_url", "kind", nullable=False)
    op.alter_column("short_url", "entity_key", nullable=False)
    op.drop_constraint("uq_short_url_pubkey_fingerprint", "short_url", type_="unique")
    op.drop_column("short_url", "relays_fingerprint")
    op.drop_column("short_url", "pubkey")
    op.create_unique_constraint(
        "uq_short_url_kind_entity_key", "short_url", ["kind", "entity_key"]
    )


def downgrade() -> None:
    # Only profiles fit the old shape; other kinds are dropped.
    op.add_column("short_url", sa.Column("pubkey", sa.String(length=64), nullable=True))
    op.add_column(
        "short_url",
        sa.Column("relays_fingerprint", sa.String(length=64), nullable=True),
    )
    op.execute("DELETE FROM short_url WHERE kind <> 'profile'")
    # Mirrors the old relays_fingerprint(): sorted, deduped, trimmed, lowercased.
    op.execute(
        r"""
        UPDATE short_url SET
          pubkey = entity_key,
          relays_fingerprint = encode(sha256(convert_to(coalesce((
            SELECT string_agg(r, E'\n' ORDER BY r COLLATE "C")
            FROM (
              SELECT DISTINCT rtrim(lower(btrim(x)), '/') AS r
              FROM jsonb_array_elements_text(relays) AS x
            ) t
          ), ''), 'UTF8')), 'hex')
        """
    )
    op.alter_column("short_url", "pubkey", nullable=False)
    op.alter_column("short_url", "relays_fingerprint", nullable=False)
    op.drop_constraint("uq_short_url_kind_entity_key", "short_url", type_="unique")
    op.drop_column("short_url", "entity_key")
    op.drop_column("short_url", "kind")
    op.create_unique_constraint(
        "uq_short_url_pubkey_fingerprint",
        "short_url",
        ["pubkey", "relays_fingerprint"],
    )
