"""the policy owns the tier

Revision ID: c5f1a72b9d40
Revises: a1c4e7b02f19
Create Date: 2026-08-31 10:00:00.000000

`billing_plan.subscription_tier` was free text a client had to recognise, and a
user's tier was found by looking up *a plan* that granted their policy — with a
tiebreak, because several plans can grant one. The policy already is the tier,
so the string goes and the plan keeps only what it really describes: price,
period, order and copy.

Nothing re-grants. `brainstorm_nsec.scheduling_id` is untouched, so every
existing subscriber keeps exactly the policy they had, with no gap.

`scheduling.is_public` gates what reaches `/billing/plans` and defaults to
false, so the backfill has to turn it on for the policies that are already
public today: the default policy and any policy an active plan sells. Staging
has two active plans on one paid policy, which this handles by construction.

The downgrade restores `subscription_tier` from the policy name (slugified to
the old `^[a-z][a-z0-9_-]*$` shape) rather than a constant, so the column comes
back carrying the same distinctions it had.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'c5f1a72b9d40'
down_revision = 'a1c4e7b02f19'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'scheduling',
        sa.Column(
            'is_public', sa.Boolean(), server_default='false', nullable=False
        ),
    )
    op.execute(
        """
        UPDATE scheduling SET is_public = true
        WHERE is_default
           OR id IN (SELECT scheduling_id FROM billing_plan WHERE is_active)
        """
    )

    op.add_column(
        'billing_plan', sa.Column('billing_period_unit', sa.String(), nullable=True)
    )
    op.add_column(
        'billing_plan', sa.Column('billing_period_count', sa.Integer(), nullable=True)
    )
    op.add_column(
        'billing_plan',
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column('billing_plan', sa.Column('blurb', sa.String(), nullable=True))
    op.add_column(
        'billing_plan',
        sa.Column('includes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'billing_plan',
        sa.Column('excludes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_column('billing_plan', 'subscription_tier')


def downgrade() -> None:
    op.add_column(
        'billing_plan', sa.Column('subscription_tier', sa.String(), nullable=True)
    )
    # Slugify the policy name back into the old vocabulary; a name that slugs to
    # nothing falls back rather than violating the NOT NULL.
    op.execute(
        """
        UPDATE billing_plan AS p SET subscription_tier = COALESCE(
            NULLIF(
                regexp_replace(
                    lower(s.name), '[^a-z0-9_-]+', '-', 'g'
                ), ''
            ),
            'tier'
        )
        FROM scheduling AS s WHERE s.id = p.scheduling_id
        """
    )
    op.execute(
        "UPDATE billing_plan SET subscription_tier = 'tier' "
        "WHERE subscription_tier IS NULL OR subscription_tier !~ '^[a-z]'"
    )
    op.alter_column(
        'billing_plan',
        'subscription_tier',
        existing_type=sa.String(),
        nullable=False,
    )

    op.drop_column('billing_plan', 'excludes')
    op.drop_column('billing_plan', 'includes')
    op.drop_column('billing_plan', 'blurb')
    op.drop_column('billing_plan', 'sort_order')
    op.drop_column('billing_plan', 'billing_period_count')
    op.drop_column('billing_plan', 'billing_period_unit')
    op.drop_column('scheduling', 'is_public')
