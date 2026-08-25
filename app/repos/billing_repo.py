"""Data access for the billing tables."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import (
    BillingPlan,
    BrainstormNsec,
    FlashWebhookEvent,
    UserSubscription,
)


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


async def get_billing_plan_on_db(
    db: AsyncDBSession, *, flash_service_id: str, flash_plan_id: str
) -> BillingPlan | None:
    """The plan a Flash subscription belongs to. None if we don't map it."""
    statement = select(BillingPlan).where(
        BillingPlan.flash_service_id == flash_service_id,
        BillingPlan.flash_plan_id == flash_plan_id,
        BillingPlan.is_active.is_(True),
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def get_user_subscription_on_db(
    db: AsyncDBSession, pubkey: str
) -> UserSubscription | None:
    """Read one subscriber's record.

    Not locked here: serialisation is `lock_user_for_update_on_db`, taken
    earlier and on a row that always exists.
    """
    statement = select(UserSubscription).where(UserSubscription.pubkey == pubkey)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def upsert_user_subscription_on_db(
    db: AsyncDBSession,
    *,
    pubkey: str,
    flash_subscription_id: str,
    flash_subscriber_id: str | None,
    billing_plan_id: int,
    granted_scheduling_id: int | None,
    flash_status: str,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    next_billing_date: datetime | None,
    trial_end_date: datetime | None,
    cancel_effective_date: datetime | None,
) -> None:
    """Record what Flash says about one subscriber. One row per pubkey."""
    values = {
        "pubkey": pubkey,
        "flash_subscription_id": flash_subscription_id,
        "flash_subscriber_id": flash_subscriber_id,
        "billing_plan_id": billing_plan_id,
        "granted_scheduling_id": granted_scheduling_id,
        "flash_status": flash_status,
        "current_period_start": current_period_start,
        "current_period_end": current_period_end,
        "next_billing_date": next_billing_date,
        "trial_end_date": trial_end_date,
        "cancel_effective_date": cancel_effective_date,
        "last_synced_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "last_sync_error": None,
    }
    statement = (
        pg_insert(UserSubscription)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["pubkey"],
            set_={k: v for k, v in values.items() if k != "pubkey"},
        )
    )
    await execute_db_statement(db, statement, __name__)


async def select_entitlement_candidates_on_db(
    db: AsyncDBSession,
) -> list[UserSubscription]:
    """Every subscription still holding a granted policy.

    Deliberately unfiltered: judging which of these has actually lapsed is
    `decide_entitlement`'s job, and duplicating any part of that rule as a SQL
    predicate would give the two room to disagree. The candidate set is bounded
    by the number of paying users, so a full read costs nothing.
    """
    statement = select(UserSubscription).where(
        UserSubscription.granted_scheduling_id.is_not(None)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def clear_granted_scheduling_on_db(db: AsyncDBSession, pubkey: str) -> None:
    """Forget what we granted, once it has been taken back."""
    statement = (
        update(UserSubscription)
        .where(UserSubscription.pubkey == pubkey)
        .values(granted_scheduling_id=None)
    )
    await execute_db_statement(db, statement, __name__)


async def select_reconcile_candidates_on_db(
    db: AsyncDBSession, *, now: datetime, stale_after: timedelta, limit: int
) -> list[UserSubscription]:
    """Subscribers whose real state only Flash can settle.

    Three groups, all of them rows where reading locally proves nothing:
    those mid-dunning, those still recorded current past the period they paid
    for, and those we simply haven't asked about in a while. Ordered oldest-read
    first so a bounded batch works through the backlog rather than re-asking
    about the same few.
    """
    statement = (
        select(UserSubscription)
        .where(
            or_(
                UserSubscription.flash_status == "past_due",
                and_(
                    UserSubscription.flash_status == "active",
                    UserSubscription.current_period_end.is_not(None),
                    UserSubscription.current_period_end <= now,
                ),
                UserSubscription.last_synced_at.is_(None),
                UserSubscription.last_synced_at <= now - stale_after,
            )
        )
        .order_by(UserSubscription.last_synced_at.asc().nullsfirst())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def record_sync_failure_on_db(
    db: AsyncDBSession, pubkey: str, reason: str
) -> None:
    """Note that we could not read Flash for this subscriber.

    Stamps `last_synced_at` even though nothing synced, because the candidate
    query orders by it: leaving it stale would park a permanently-failing
    subscriber at the head of a bounded batch forever, starving everyone behind
    them. They come back on the normal staleness cadence instead, and
    `last_sync_error` is what says the last attempt failed.
    """
    statement = (
        update(UserSubscription)
        .where(UserSubscription.pubkey == pubkey)
        .values(
            last_sync_error=reason,
            last_synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await execute_db_statement(db, statement, __name__)


async def lock_user_for_update_on_db(
    db: AsyncDBSession, pubkey: str, *, skip_locked: bool = False
) -> bool:
    """Serialise everything that reconciles one subscriber. True if we hold it.

    Locks `brainstorm_nsec` rather than `user_subscription` because it always
    exists by this point, where the subscription row may not — and SELECT FOR
    UPDATE on a row that isn't there locks nothing, so two first-time events for
    the same person would sail past each other.

    `skip_locked` returns False instead of waiting. Background work uses it so
    it never queues behind, or in front of, a live webhook: Flash allows ten
    seconds to acknowledge, and this lock is held across a Flash read.
    """
    statement = (
        select(BrainstormNsec.pubkey)
        .where(BrainstormNsec.pubkey == pubkey)
        .with_for_update(skip_locked=skip_locked)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None


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


async def prune_webhook_payloads_on_db(
    db: AsyncDBSession, *, older_than: datetime
) -> int:
    """Drop the payloads of old events, keeping the rows.

    Payloads carry subscriber email and name; the row itself carries the dedupe
    key and the audit trail, so both survive the personal data being removed.
    """
    statement = (
        update(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.created_at <= older_than,
            FlashWebhookEvent.payload.is_not(None),
            # Never strip an event still waiting to be applied: replay reads the
            # payload to find the subscriber, so pruning one would silently make
            # it unreplayable.
            FlashWebhookEvent.processed_at.is_not(None),
        )
        .values(payload=None)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.rowcount
