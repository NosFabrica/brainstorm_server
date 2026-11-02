"""add vouch params to graperank presets

Revision ID: a7c9e2f41b05
Revises: c5f1d7a9e2b4, d4e5f6a7b8c9
Create Date: 2026-11-03 09:30:00.000000

Vouches (kind 30003, d="vouches") join follows, mutes and reports as a rating
kind in GrapeRank: a deliberate, reputationally costly endorsement rather than
ambient attention. Adds vouch_rating / vouch_confidence to both preset tables
and sets the factory values per preset:

- DEFAULT / RESTRICTIVE: rating 1.0, confidence 0.4 — an order of magnitude
  above a bare follow (0.03), below the Observer's own follow (0.5).
- PERMISSIVE: rating 1.0, confidence 0.5 — mirrors the preset's general
  inflation of edge confidences.

This migration also merges the two outstanding heads (c5f1d7a9e2b4,
d4e5f6a7b8c9): tests/test_alembic_heads.py requires a single head and start.sh
runs `alembic upgrade head` under `set -e`.
"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c9e2f41b05"
down_revision = ("c5f1d7a9e2b4", "d4e5f6a7b8c9")
branch_labels = None
depends_on = None


_FACTORY = {
    "DEFAULT": (1.0, 0.4),
    "PERMISSIVE": (1.0, 0.5),
    "RESTRICTIVE": (1.0, 0.4),
}

_EXISTING_FIELDS = [
    "rigor",
    "attenuation_factor",
    "follow_rating",
    "follow_confidence",
    "mute_rating",
    "mute_confidence",
    "report_rating",
    "report_confidence",
    "follow_confidence_of_observer",
    "verified_followers_influence_cutoff",
    "verified_reporters_influence_cutoff",
    "verified_muters_influence_cutoff",
]


def upgrade() -> None:
    for table in ("graperank_preset", "graperank_preset_history"):
        op.add_column(
            table, sa.Column("vouch_rating", sa.Float(), nullable=True)
        )
        op.add_column(
            table, sa.Column("vouch_confidence", sa.Float(), nullable=True)
        )

    for preset_id, (rating, confidence) in _FACTORY.items():
        op.execute(
            f"UPDATE graperank_preset SET vouch_rating = {rating}, "
            f"vouch_confidence = {confidence} WHERE id = '{preset_id}'"
        )
    # History rows written before this migration have no vouch values; fill the
    # DEFAULT factory values so the NOT NULL constraint holds. They are
    # audit-only — the Java worker never reads history.
    op.execute(
        "UPDATE graperank_preset_history SET vouch_rating = 1.0, "
        "vouch_confidence = 0.4 WHERE vouch_rating IS NULL"
    )

    for table in ("graperank_preset", "graperank_preset_history"):
        op.alter_column(table, "vouch_rating", nullable=False)
        op.alter_column(table, "vouch_confidence", nullable=False)

    # One UPDATE history row per preset, snapshotting the full post-migration
    # state, so the audit log explains where the vouch values came from.
    cols = ", ".join(_EXISTING_FIELDS + ["vouch_rating", "vouch_confidence"])
    op.execute(
        f"INSERT INTO graperank_preset_history "
        f"(preset_id, {cols}, change_type, changed_by) "
        f"SELECT id, {cols}, 'UPDATE', NULL FROM graperank_preset"
    )


def downgrade() -> None:
    # The history rows this migration added stay: the log is append-only, and a
    # downgrade is no reason to rewrite the audit trail.
    for table in ("graperank_preset_history", "graperank_preset"):
        op.drop_column(table, "vouch_confidence")
        op.drop_column(table, "vouch_rating")
