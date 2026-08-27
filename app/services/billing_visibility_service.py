"""What nobody has settled — the states a human has to resolve.

Everything here is a disagreement between three parties: Flash took the money,
our record says what we believe, and the scheduling assignment says what the
subscriber actually receives. Where those diverge is the bug, and the point of
gathering them is that it should be findable by sorting a column rather than by
waiting for a complaint.
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.repos.flash_webhook_event_repo import (
    select_exhausted_events_on_db,
    select_payment_history_on_db,
    select_unresolved_events_on_db,
)
from app.repos.user_subscription_repo import (
    AbandonRule,
    select_abandoned_checkouts_on_db,
    select_failing_syncs_on_db,
    select_policy_mismatches_on_db,
    select_stale_syncs_on_db,
    select_unrecognised_statuses_on_db,
)
from app.services.billing_service import (
    CANCELLED_STATUS,
    ENDED_STATUSES,
    ENTITLING_STATUSES,
    EntitlementReason,
    utc_now,
)

# Everything decide_entitlement knows how to act on. Anything else is a status
# Flash has started sending that we hold subscribers on indefinitely.
KNOWN_STATUSES = sorted(
    ENTITLING_STATUSES | ENDED_STATUSES | {CANCELLED_STATUS, "past_due", "pending"}
)


# Every query is capped: a report nobody can open is no use on the day it
# matters, and these are read while something is already wrong.
ROW_LIMIT = 200


@dataclass(frozen=True)
class DivergenceReport:
    """Seven kinds of disagreement, plus admin overrides and abandoned
    checkouts — which are not faults, but must be visible somewhere, and must
    not be *here*, or a genuine failed write hides among them."""

    policy_mismatch: list
    admin_overrides: list
    stale_syncs: list
    failing_syncs: list
    unresolved_events: list
    unrecognised_statuses: list
    exhausted_events: list
    abandoned_checkouts: list


def _section(rows: list) -> dict:
    return {
        "count": len(rows),
        "truncated": len(rows) >= ROW_LIMIT,
        "rows": [dict(row._mapping) for row in rows],
    }


async def build_divergence_response(db: AsyncDBSession) -> dict[str, dict]:
    """The report as the API returns it.

    Keys are written out rather than reflected off the dataclass: a renamed
    field would otherwise quietly rename a JSON key that a caller depends on.
    """
    report = await build_divergence_report(db)
    return {
        "policy_mismatch": _section(report.policy_mismatch),
        "admin_overrides": _section(report.admin_overrides),
        "stale_syncs": _section(report.stale_syncs),
        "failing_syncs": _section(report.failing_syncs),
        "unresolved_events": _section(report.unresolved_events),
        "unrecognised_statuses": _section(report.unrecognised_statuses),
        "exhausted_events": _section(report.exhausted_events),
        "abandoned_checkouts": _section(report.abandoned_checkouts),
    }


async def build_divergence_report(db: AsyncDBSession) -> DivergenceReport:
    now = utc_now()
    stale_before = now - timedelta(hours=settings.billing_stale_sync_hours)
    abandoned = AbandonRule(
        after=timedelta(seconds=settings.billing_abandon_pending_after_seconds),
        error=EntitlementReason.UNKNOWN_SUBSCRIPTION.value,
    )
    return DivergenceReport(
        policy_mismatch=await select_policy_mismatches_on_db(
            db, admin_held=False, limit=ROW_LIMIT
        ),
        admin_overrides=await select_policy_mismatches_on_db(
            db, admin_held=True, limit=ROW_LIMIT
        ),
        stale_syncs=await select_stale_syncs_on_db(
            db, older_than=stale_before, limit=ROW_LIMIT, now=now, abandoned=abandoned
        ),
        failing_syncs=await select_failing_syncs_on_db(
            db, limit=ROW_LIMIT, now=now, abandoned=abandoned
        ),
        unresolved_events=await select_unresolved_events_on_db(db, limit=ROW_LIMIT),
        unrecognised_statuses=await select_unrecognised_statuses_on_db(
            db, known=KNOWN_STATUSES
        ),
        exhausted_events=await select_exhausted_events_on_db(
            db, max_attempts=settings.billing_replay_max_attempts, limit=ROW_LIMIT
        ),
        abandoned_checkouts=await select_abandoned_checkouts_on_db(
            db, limit=ROW_LIMIT, now=now, abandoned=abandoned
        ),
    )


PAYMENT_COLUMNS = (
    "paid_at",
    "event",
    "pubkey",
    "subscription_id",
    "invoice_id",
    "amount_minor",
    "currency",
)


async def build_payment_history_csv(
    db: AsyncDBSession, *, since: datetime, until: datetime, limit: int
) -> str:
    """Payments for accounting, read out of the stored renewal events.

    Not a second ledger: Flash took the money and is authoritative about it, so
    this reports what Flash told us rather than keeping a parallel record that
    could disagree with it. First charges appear as `subscription.activated`
    rows priced from the plan — that event carries no amount of its own.
    """
    rows = await select_payment_history_on_db(
        db, since=since, until=until, limit=limit
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(PAYMENT_COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow({column: getattr(row, column, None) for column in PAYMENT_COLUMNS})
    return buffer.getvalue()
