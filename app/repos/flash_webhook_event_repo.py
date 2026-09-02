"""Data access for `flash_webhook_event` — the inbox, not a ledger. Never read
to decide whether someone is paid; that comes from Flash's API."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    String,
    Text,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import FlashWebhookEvent


async def insert_flash_webhook_event_on_db(
    db: AsyncDBSession,
    *,
    event: str,
    delivery_timestamp: int,
    event_timestamp: datetime | None,
    subscription_id: str | None,
    payload: dict,
    dedupe_key: str,
    claimed_at: datetime | None = None,
) -> int | None:
    """Record one delivery. Returns its id, or None if we already had it.

    `claimed_at` marks it as already being worked on. The webhook path passes it
    because it is about to do exactly that — without it the row looks abandoned
    from the moment it lands, and the recovery sweep would duplicate every
    delivery still in flight.

    ON CONFLICT DO NOTHING rather than catching IntegrityError: a raised
    constraint violation poisons the transaction, and this runs on the path that
    must still commit and answer 200 so Flash stops retrying.
    """
    statement = (
        pg_insert(FlashWebhookEvent)
        .values(
            event=event,
            delivery_timestamp=delivery_timestamp,
            event_timestamp=event_timestamp,
            subscription_id=subscription_id,
            payload=payload,
            dedupe_key=dedupe_key,
            processing_started_at=claimed_at,
            attempts=1 if claimed_at else 0,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(FlashWebhookEvent.id)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()



async def select_abandoned_webhook_events_on_db(
    db: AsyncDBSession,
    *,
    now: datetime,
    stale_after: timedelta,
    max_attempts: int,
    limit: int,
) -> list[FlashWebhookEvent]:
    """Events we acknowledged and then never finished.

    The staleness window is what separates "a worker has this" from "a worker
    died holding this" — without it the sweep would fight live processing.
    """
    statement = (
        select(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.processed_at.is_(None),
            FlashWebhookEvent.attempts < max_attempts,
            or_(
                FlashWebhookEvent.processing_started_at.is_(None),
                FlashWebhookEvent.processing_started_at <= now - stale_after,
            ),
        )
        .order_by(FlashWebhookEvent.created_at.asc())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def claim_webhook_event_on_db(
    db: AsyncDBSession, event_id: int, *, now: datetime, stale_after: timedelta
) -> bool:
    """Take ownership of one event. False means someone else got there first.

    The WHERE clause re-checks what the select found, so the claim is decided by
    the database rather than by whoever read first — which is what makes replay
    exactly-once with several replicas running.
    """
    statement = (
        update(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.id == event_id,
            FlashWebhookEvent.processed_at.is_(None),
            or_(
                FlashWebhookEvent.processing_started_at.is_(None),
                FlashWebhookEvent.processing_started_at <= now - stale_after,
            ),
        )
        .values(
            processing_started_at=now,
            attempts=FlashWebhookEvent.attempts + 1,
        )
        .returning(FlashWebhookEvent.id)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None


async def mark_webhook_event_processed_on_db(
    db: AsyncDBSession, event_id: int, *, now: datetime
) -> None:
    statement = (
        update(FlashWebhookEvent)
        .where(FlashWebhookEvent.id == event_id)
        .values(processed_at=now, process_error=None)
    )
    await execute_db_statement(db, statement, __name__)


async def settle_unresolved_events_on_db(
    db: AsyncDBSession,
    *,
    subscription_id: str,
    now: datetime,
    resolution: str,
    resolved_by: str,
) -> int:
    """Mark every still-open delivery for one subscription decided by hand.

    Every delivery, not one: a plain-link signup that also renewed has more than
    one event carrying the same unattributable id, and leaving the siblings open
    would keep the sweep re-checking a subscription somebody has already
    resolved.

    Writing `processed_at` is what stops that re-checking, and it is also what
    makes the row prunable — an unattributed event is never processed, so its
    payload keeps the subscriber's email for as long as it takes to match them,
    and ages out normally from the moment it is settled.
    """
    statement = (
        update(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.subscription_id == subscription_id,
            FlashWebhookEvent.processed_at.is_(None),
        )
        .values(
            processed_at=now,
            process_error=None,
            resolution=resolution,
            resolved_by=resolved_by,
        )
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.rowcount


async def record_webhook_event_failure_on_db(
    db: AsyncDBSession, event_id: int, reason: str
) -> None:
    """Leaves processed_at null, so it comes back round once the claim goes stale."""
    statement = (
        update(FlashWebhookEvent)
        .where(FlashWebhookEvent.id == event_id)
        .values(process_error=reason)
    )
    await execute_db_statement(db, statement, __name__)


# Two failures wore one name here: a delivery that named nobody and a delivery
# naming a plan we never mapped share only the fact that they did not apply.
# They are selected separately so each count means one thing, and together they
# cover exactly the rows one query used to return.
_UNAPPLIED = (
    FlashWebhookEvent.processed_at.is_(None),
    FlashWebhookEvent.process_error.is_not(None),
)


def _unattributed():
    """No reference at all. An empty one is as absent as a missing one — that is
    how the entitlement path reads it, and the two must agree on which rows
    count as unattributed."""
    ref = FlashWebhookEvent.payload["data"]["externalRef"].astext
    return or_(ref.is_(None), ref == "")


async def select_unresolved_signups_on_db(db: AsyncDBSession, *, limit: int) -> list:
    """Payments that named nobody — to be attributed to a person, or dismissed.

    Flash's webhook payload carries no contact details — verified against every
    event we hold, and matching the documented schema — so there is nothing here
    to identify the payer by. The Flash subscription id is the only handle, and
    who is behind it is visible only in Flash's own dashboard.
    """
    statement = (
        select(
            FlashWebhookEvent.id,
            FlashWebhookEvent.event,
            FlashWebhookEvent.created_at,
            FlashWebhookEvent.process_error,
            # Named as the rest of the codebase names it: the admin view turns
            # this key into a link into Flash. It is also the only handle these
            # rows have — attribute and dismiss both act on it.
            FlashWebhookEvent.subscription_id.label("flash_subscription_id"),
        )
        .where(*_UNAPPLIED, _unattributed())
        .order_by(FlashWebhookEvent.created_at.desc())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_unmapped_plan_events_on_db(db: AsyncDBSession, *, limit: int) -> list:
    """Deliveries that named a subscriber and still failed — almost always a
    plan we never mapped.

    Nobody has to be identified here, so the row carries the service/plan pair
    an admin would map and nothing personal. Creating that mapping is the whole
    fix: `reset_events_awaiting_plan_on_db` hands these back to the replay pass.
    `process_error` rides along for the rarer cause — a Flash read that failed
    for its own reasons — which the same section would otherwise disguise.
    """
    payload = FlashWebhookEvent.payload["data"]
    statement = (
        select(
            FlashWebhookEvent.id,
            FlashWebhookEvent.event,
            FlashWebhookEvent.created_at,
            FlashWebhookEvent.process_error,
            FlashWebhookEvent.subscription_id.label("flash_subscription_id"),
            payload["externalRef"].astext.label("external_ref"),
            payload["serviceId"].astext.label("flash_service_id"),
            payload["planId"].astext.label("flash_plan_id"),
        )
        .where(*_UNAPPLIED, not_(_unattributed()))
        .order_by(FlashWebhookEvent.created_at.desc())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def reset_events_awaiting_plan_on_db(
    db: AsyncDBSession, *, flash_service_id: str, flash_plan_id: str, error: str
) -> int:
    """Make the events that failed for want of this plan replayable again.

    Creating the mapping is otherwise only half a fix: the events that hit the
    missing plan have already spent their attempts, so a paying subscriber would
    stay unentitled until their next renewal. Clearing both the error and the
    attempt count hands them back to the replay pass.

    Narrowed to rows that failed for exactly this reason, so a delivery held up
    by something else does not get a free extra life. `processing_started_at` is
    left alone: a claim someone is currently working is still theirs.
    """
    payload = FlashWebhookEvent.payload["data"]
    statement = (
        update(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.processed_at.is_(None),
            FlashWebhookEvent.process_error == error,
            payload["serviceId"].astext == flash_service_id,
            payload["planId"].astext == flash_plan_id,
        )
        .values(attempts=0, process_error=None)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.rowcount


async def select_exhausted_events_on_db(
    db: AsyncDBSession, *, max_attempts: int, limit: int
) -> list:
    """Events that ran out of replay attempts and are now nobody's job."""
    statement = select(
        FlashWebhookEvent.id,
        FlashWebhookEvent.event,
        FlashWebhookEvent.attempts,
        FlashWebhookEvent.process_error,
    ).where(
        FlashWebhookEvent.processed_at.is_(None),
        FlashWebhookEvent.attempts >= max_attempts,
    ).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())
