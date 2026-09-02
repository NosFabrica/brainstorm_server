"""Data access for `user_subscription` — one row per subscriber, plus the
divergence reads that compare it against the live scheduling assignment."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, and_, case, func, not_, or_, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.core.flash import FlashSubscription
from app.db_models import (
    BillingPlan,
    BrainstormNsec,
    Scheduling,
    SchedulingSource,
    UserSubscription,
)


@dataclass(frozen=True)
class AbandonRule:
    """A checkout nobody finished, in one place.

    Flash discards a pending checkout whose payment never confirms, so the
    subscription stops existing on their side and every lookup answers with
    nothing. Those rows are real and stay, but there is no longer anything to
    ask about — so the sweep skips them and the two report sections that mean
    "someone look at this" leave them out.

    One definition because it has four users pulling in opposite directions:
    negated by the sweep and by both report sections, asserted by the section
    that counts them. Written twice, they would drift and a row would be both
    written off and still alarming.
    """

    after: timedelta
    error: str

    def condition(self, now: datetime):
        return and_(
            # Never granted anything, so nothing is at stake in letting it go.
            # The same error against a row that DID grant is an anomaly, and
            # has to stay loud however long it has been true.
            UserSubscription.flash_status == "pending",
            UserSubscription.granted_scheduling_id.is_(None),
            # Flash answered and had nothing. Being unable to ask — an outage,
            # a credential failure — raises instead, and must keep retrying.
            UserSubscription.last_sync_error == self.error,
            UserSubscription.sync_error_since.is_not(None),
            UserSubscription.sync_error_since <= now - self.after,
        )

    def matches(self, row, now: datetime) -> bool:
        """The same question, asked of a row already in hand.

        Deliberately beside `condition` rather than wherever it was needed: the
        sweep asks in SQL and the user-facing view asks in Python, and the two
        answering differently is how a subscriber ends up written off in one
        place and still "confirming your payment" in the other.
        """
        return (
            row.flash_status == "pending"
            and row.granted_scheduling_id is None
            and row.last_sync_error == self.error
            and row.sync_error_since is not None
            and row.sync_error_since <= now - self.after
        )


def settled_condition():
    """A subscription that has run its course, with nothing left to ask about.

    `expired` is the one status Flash's state machine has no exit from: a
    subscriber who comes back gets a new subscription id, which arrives as an
    `activated` webhook and upserts the row outright. So re-reading these can
    only ever return `expired` again — once per sweep, per churned subscriber,
    for as long as the row exists.

    Narrow on three counts. Only `expired`, never `paused` or `canceled`:
    a pause is reversible by definition and a cancellation is still on its way
    to expiring, so both are rows where asking Flash still does work. Only where
    nothing is granted, so a tier cannot be left standing on a dead
    subscription. And only with no outstanding error, because a row we stop
    reading can never clear one — it would sit in `failing_syncs` for good.
    """
    return and_(
        UserSubscription.flash_status == "expired",
        UserSubscription.granted_scheduling_id.is_(None),
        UserSubscription.last_sync_error.is_(None),
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


async def get_user_subscription_by_flash_id_on_db(
    db: AsyncDBSession, flash_subscription_id: str
) -> UserSubscription | None:
    """Whoever already holds one Flash subscription, if anyone does.

    The other direction of the same table, and the only way to tell an
    unattributed signup from one that has already been resolved — the pubkey is
    exactly what an unattributed one does not have.
    """
    statement = select(UserSubscription).where(
        UserSubscription.flash_subscription_id == flash_subscription_id
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalars().first()


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
        "portal_url": subscription.portal_url,
        "last_synced_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "last_sync_error": None,
        "sync_error_since": None,
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


async def count_subscriptions_for_plan_on_db(
    db: AsyncDBSession, billing_plan_id: int
) -> int:
    """How many people bought this mapping. Zero is what makes its Flash ids
    editable — rewriting them under a subscriber would retroactively change
    what that person bought."""
    statement = select(func.count()).where(
        UserSubscription.billing_plan_id == billing_plan_id
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one()


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
    db: AsyncDBSession,
    *,
    now: datetime,
    stale_after: timedelta,
    limit: int,
    abandoned: AbandonRule,
) -> list[UserSubscription]:
    """Subscribers whose real state only Flash can settle.

    Five groups, all of them rows where reading locally proves nothing:
    those mid-dunning, those whose checkout never confirmed, those still
    recorded current past the period they paid for, those about to renew, and
    those we simply haven't asked about in a while. Ordered oldest-read first so
    a bounded batch works through the backlog rather than re-asking about the
    same few.

    Minus one group that will never settle: a checkout that never confirmed and
    that Flash has since forgotten. Re-reading it can only return the same
    answer, and the paths that could revive the subscriber — an `activated`
    webhook, a refresh, an operator resync — all bypass this query.

    Narrow on purpose. Only where nothing was ever granted, so nothing is at
    stake; only for the error meaning Flash answered and had nothing, never one
    meaning we could not ask; and only once it has said so for the whole window,
    measured from `sync_error_since` — how long *this failure* has run, not how
    old the row is. A subscriber can sit legitimately pending for weeks, so row
    age would write them off on their first blip.
    `select_failing_syncs_on_db` keeps them visible after we stop asking.

    Minus one more that has already settled: an expired subscription holding no
    policy. The stale clause below would otherwise re-read every subscriber who
    ever churned, once per cycle, forever — a cost that only grows, for an
    answer that cannot change. See `settled_condition`.
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
            ),
            not_(abandoned.condition(now)),
            not_(settled_condition()),
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    statement = (
        update(UserSubscription)
        .where(UserSubscription.pubkey == pubkey)
        .values(
            last_sync_error=reason,
            last_synced_at=now,
            # Only a *change* of reason restarts the clock. The same reason
            # repeating is the same failure continuing, and re-stamping it would
            # make a permanent failure look permanently fresh.
            sync_error_since=case(
                (
                    and_(
                        UserSubscription.last_sync_error == reason,
                        UserSubscription.sync_error_since.is_not(None),
                    ),
                    UserSubscription.sync_error_since,
                ),
                else_=now,
            ),
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
            UserSubscription.current_period_start,
            UserSubscription.current_period_end,
            UserSubscription.next_billing_date,
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
    db: AsyncDBSession,
    *,
    older_than: datetime,
    limit: int,
    now: datetime,
    abandoned: AbandonRule,
) -> list:
    """Subscribers we have not read from Flash recently enough to trust.

    Abandoned checkouts and settled subscriptions are both excluded because
    *we* stopped reading them: their `last_synced_at` is frozen by design, so
    they would otherwise every one of them age into this section within a day
    and never leave.
    """
    statement = select(
        UserSubscription.pubkey,
        UserSubscription.flash_status,
        UserSubscription.last_synced_at,
    ).where(
        or_(
            UserSubscription.last_synced_at.is_(None),
            UserSubscription.last_synced_at <= older_than,
        ),
        not_(abandoned.condition(now)),
        not_(settled_condition()),
    ).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_failing_syncs_on_db(
    db: AsyncDBSession, *, limit: int, now: datetime, abandoned: AbandonRule
) -> list:
    """Subscribers whose last read from Flash failed and still matters.

    Bounded and unordered, so which rows come back is arbitrary once there are
    more than `limit` of them. Abandoned checkouts are the one failure that is
    both expected and unbounded in number — left in, they would displace the
    credential error or the lost paying subscriber this section exists to show.
    """
    statement = select(
        UserSubscription.pubkey,
        UserSubscription.last_sync_error,
        UserSubscription.last_synced_at,
    ).where(
        UserSubscription.last_sync_error.is_not(None),
        not_(abandoned.condition(now)),
    ).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_abandoned_checkouts_on_db(
    db: AsyncDBSession, *, limit: int, now: datetime, abandoned: AbandonRule
) -> list:
    """Checkouts that were started, never paid for, and given up on.

    Individually unremarkable — Flash documents the discard. Worth a section of
    their own so the count is visible: a spike is not a billing fault but a
    broken checkout, which nothing else in this report would show.
    """
    statement = select(
        UserSubscription.pubkey,
        UserSubscription.flash_subscription_id,
        UserSubscription.sync_error_since,
    ).where(abandoned.condition(now)).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_retired_plan_subscribers_on_db(
    db: AsyncDBSession, *, limit: int
) -> list:
    """Subscribers still on a plan we have withdrawn from sale.

    Not a fault — retiring a plan is an ordinary operation and these people keep
    renewing exactly as before. Visible because nothing else would say they
    exist: they are being charged for something no one can buy any more, and
    ending that is a decision a human takes, in Flash, one subscription at a
    time. `flash_subscription_id` is here so the admin view can link straight to
    it; we have no cancel of our own to offer, and must not appear to.

    Settled rows are excluded for the same reason they are everywhere else —
    a churned subscriber on a retired plan is nobody's outstanding work.
    """
    statement = (
        select(
            UserSubscription.pubkey,
            UserSubscription.flash_subscription_id,
            UserSubscription.flash_status,
            UserSubscription.billing_plan_id,
            UserSubscription.granted_scheduling_id,
        )
        .join(BillingPlan, BillingPlan.id == UserSubscription.billing_plan_id)
        .where(BillingPlan.is_active.is_(False), not_(settled_condition()))
        .limit(limit)
    )
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


