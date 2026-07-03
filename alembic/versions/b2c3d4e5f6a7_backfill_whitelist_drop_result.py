"""backfill observerwhitelist from result, then drop brainstorm_request.result

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-03 00:00:00.000000

DESTRUCTIVE. Backfills observerwhitelist for each observer's latest SUCCESS run
by extracting above-cutoff {observee: round(influence,2)} from the ~100MB result
blob entirely server-side (no blob enters this process), then drops the column.

NOTE: DROP COLUMN is metadata-only. It does NOT reclaim the ~2GB of dead space
(~99MB/row). Reclaim separately and deliberately afterwards:
  VACUUM FULL brainstorm_request;      -- locks the table
  -- or, online:  pg_repack -t brainstorm_request
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None

# Must match settings.cutoff_of_valid_graperank_scores at deploy time.
CUTOFF = 0.02


_BACKFILL = sa.text(
    """
    INSERT INTO observerwhitelist (observer_pubkey, scores, last_request_id, created_at, updated_at)
    SELECT br.pubkey,
           COALESCE(
               (SELECT jsonb_object_agg(k, round((v->>'influence')::numeric, 2))
                  FROM jsonb_each((br.result)::jsonb -> 'scorecards') AS s(k, v)
                 WHERE round((v->>'influence')::numeric, 2) >= :cutoff),
               '{}'::jsonb
           ),
           br.private_id, now(), now()
    FROM (
        SELECT DISTINCT ON (pubkey) private_id, pubkey, result
        FROM brainstorm_request
        WHERE status = 'success'
          AND pubkey IS NOT NULL
          AND result IS NOT NULL
        ORDER BY pubkey, created_at DESC
    ) br
    ON CONFLICT (observer_pubkey) DO UPDATE
        SET scores = EXCLUDED.scores,
            last_request_id = EXCLUDED.last_request_id,
            updated_at = now();
    """
)


def upgrade() -> None:
    op.execute(_BACKFILL.bindparams(cutoff=CUTOFF))
    op.drop_column('brainstorm_request', 'result')


def downgrade() -> None:
    # Structural only — the dropped blobs are not recoverable.
    op.add_column(
        'brainstorm_request',
        sa.Column('result', sa.String(), nullable=True),
    )
