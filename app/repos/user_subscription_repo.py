"""Data access for `user_subscription` — one row per subscriber, plus the
divergence reads that compare it against the live scheduling assignment."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.core.flash import FlashSubscription
from app.db_models import (
    BrainstormNsec,
    Scheduling,
    SchedulingSource,
    UserSubscription,
)


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
    subscription: FlashSubscription,
    billing_plan_id: int,
    granted_scheduling_id: int | None,
) -> None:
    """Record what Flash says about one subscriber. One row per pubkey."""
    values = {
        "pubkey": pubkey,
        "flash_subscription_id": subscription.id,
        "flash_subscriber_id": subscription.subscriber_id,
        "billing_plan_id": billing_plan_id,
        "granted_scheduling_id": granted_scheduling_id,
        "flash_status": subscription.status,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "next_billing_date": subscription.next_billing_date,
        "trial_end_date": subscription.trial_end_date,
        "cancel_effective_date": subscription.cancel_effective_date,
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


async def update_flash_status_on_db(
    db: AsyncDBSession,
    pubkey: str,
    status: str,
    *,
    cancel_effective_date: datetime | None = None,
) -> None:
    """Record a status implied by a payload, when Flash itself cannot be read.

    Deliberately does not stamp `last_synced_at` — nothing synced, and the
    reconcile loop should still treat this row as needing Flash's own answer.
    `cancel_effective_date` is written only when given (a fallback `canceled`
    materializes when the entitlement ends).
    """
    values: dict = {"flash_status": status}
    if cancel_effective_date is not None:
        values["cancel_effective_date"] = cancel_effective_date
    statement = (
        update(UserSubscription)
        .where(UserSubscription.pubkey == pubkey)
        .values(**values)
    )
    await execute_db_statement(db, statement, __name__)


async def update_last_event_at_on_db(
    db: AsyncDBSession, *, pubkey: str | None, event_timestamp: datetime | None
) -> None:
    """Advance the newest-event marker. Never moves backwards — a replayed old
    event must not make the record look staler than it is."""
    if not pubkey or event_timestamp is None:
        return
    statement = (
        update(UserSubscription)
        .where(
            UserSubscription.pubkey == pubkey,
            or_(
                UserSubscription.last_event_at.is_(None),
                UserSubscription.last_event_at < event_timestamp,
            ),
        )
        .values(last_event_at=event_timestamp)
    )
    await execute_db_statement(db, statement, __name__)


async def select_reconcile_candidates_on_db(
    db: AsyncDBSession, *, now: datetime, stale_after: timedelta, limit: int
) -> list[UserSubscription]:
    """Subscribers whose real state only Flash can settle.

    Five groups, all of them rows where reading locally proves nothing:
    those mid-dunning, those whose checkout never confirmed, those still
    recorded current past the period they paid for, those about to renew, and
    those we simply haven't asked about in a while. Ordered oldest-read first so
    a bounded batch works through the backlog rather than re-asking about the
    same few.
    """
    statement = (
        select(UserSubscription)
        .where(
            or_(
                UserSubscription.flash_status.in_(("past_due", "pending")),
                and_(
                    UserSubscription.flash_status == "active",
                    UserSubscription.current_period_end.is_not(None),
                    UserSubscription.current_period_end <= now,
                ),
                # Renewing within the hour: a missed `renewed` event here is
                # what turns into a spurious lapse at the period boundary.
                and_(
                    UserSubscription.next_billing_date.is_not(None),
                    UserSubscription.next_billing_date <= now + timedelta(hours=1),
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



def build_billing_subscriptions_stmt() -> Select:
    """Every subscriber, with what Flash says and what they actually receive.

    Those are two different questions and the whole surface exists to keep them
    apart, so the policy is joined twice: once through the grant we recorded,
    once through the assignment the scheduler will actually honour.
    """
    granted = aliased(Scheduling)
    actual = aliased(Scheduling)
    return (
        select(
            UserSubscription.pubkey,
            UserSubscription.flash_status,
            UserSubscription.current_period_end,
            UserSubscription.last_synced_at,
            UserSubscription.last_sync_error,
            UserSubscription.flash_subscription_id,
            UserSubscription.granted_scheduling_id,
            granted.name.label("granted_scheduling_name"),
            BrainstormNsec.scheduling_id,
            actual.name.label("scheduling_name"),
            BrainstormNsec.scheduling_source,
            BrainstormNsec.billing_blocked,
        )
        .join(BrainstormNsec, BrainstormNsec.pubkey == UserSubscription.pubkey)
        .outerjoin(granted, granted.id == UserSubscription.granted_scheduling_id)
        .outerjoin(actual, actual.id == BrainstormNsec.scheduling_id)
        .order_by(UserSubscription.created_at.desc())
    )


async def select_policy_mismatches_on_db(
    db: AsyncDBSession, *, admin_held: bool, limit: int
) -> list:
    """Subscribers receiving something other than what we recorded granting.

    This is the bug the two columns exist to make visible: someone paying who
    is not on the paid cadence, or someone on it who stopped paying.

    `admin_held` splits the two cases rather than dropping one. An admin
    assignment differing from what billing granted is a human's decision, not a
    fault — but since granting to a comped user now preserves their `admin`
    source, excluding those outright would also hide a genuinely failed write.
    """
    statement = (
        select(
            UserSubscription.pubkey,
            UserSubscription.flash_status,
            UserSubscription.granted_scheduling_id,
            BrainstormNsec.scheduling_id,
            BrainstormNsec.scheduling_source,
        )
        .join(BrainstormNsec, BrainstormNsec.pubkey == UserSubscription.pubkey)
        .where(
            (
                BrainstormNsec.scheduling_source == SchedulingSource.ADMIN.value
                if admin_held
                else BrainstormNsec.scheduling_source != SchedulingSource.ADMIN.value
            ),
            UserSubscription.granted_scheduling_id.is_not(None),
            or_(
                BrainstormNsec.scheduling_id.is_(None),
                BrainstormNsec.scheduling_id != UserSubscription.granted_scheduling_id,
            ),
        )
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_stale_syncs_on_db(
    db: AsyncDBSession, *, older_than: datetime, limit: int
) -> list:
    """Subscribers we have not read from Flash recently enough to trust."""
    statement = select(
        UserSubscription.pubkey,
        UserSubscription.flash_status,
        UserSubscription.last_synced_at,
    ).where(
        or_(
            UserSubscription.last_synced_at.is_(None),
            UserSubscription.last_synced_at <= older_than,
        )
    ).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_failing_syncs_on_db(db: AsyncDBSession, *, limit: int) -> list:
    """Subscribers whose last read from Flash failed."""
    statement = select(
        UserSubscription.pubkey,
        UserSubscription.last_sync_error,
        UserSubscription.last_synced_at,
    ).where(UserSubscription.last_sync_error.is_not(None)).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_unrecognised_statuses_on_db(
    db: AsyncDBSession, *, known: list[str]
) -> list:
    """Statuses Flash has sent that we have no mapping for.

    Flash documents its set as open, so this is how a new one surfaces rather
    than silently holding every affected subscriber forever.
    """
    statement = (
        select(UserSubscription.flash_status, func.count().label("subscribers"))
        .where(UserSubscription.flash_status.not_in(known))
        .group_by(UserSubscription.flash_status)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


