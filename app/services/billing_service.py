"""Turning Flash subscription state into a scheduling policy.

Entitlement *is* the scheduling assignment; the `user_subscription` row only
records why. Two rules govern everything here: nothing is granted from an event
body (Flash's own view is read first, which also makes concurrent processing
converge), and uncertainty never costs a user their tier — an unreachable Flash,
an unrecognised status or an unmapped plan all leave the policy alone.
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.flash import (
    FlashCredentialError,
    FlashSubscription,
    FlashUnavailable,
    fetch_subscription,
)
from app.core.loggr import loggr
from app.db_models import BillingPlan, SchedulingSource
from app.repos.billing_repo import (
    clear_granted_scheduling_on_db,
    get_billing_plan_on_db,
    get_user_subscription_for_update_on_db,
    record_sync_failure_on_db,
    select_entitlement_candidates_on_db,
    select_reconcile_candidates_on_db,
    upsert_user_subscription_on_db,
)
from app.repos.brainstorm_nsec import (
    brainstorm_nsec_exists_by_pubkey_on_db,
    get_scheduling_source_on_db,
    is_billing_blocked_on_db,
    set_scheduling_for_pubkey_on_db,
)
from app.repos.scheduling_repo import get_scheduling_on_db

logger = loggr.get_logger(__name__)

# Flash's status set is documented as OPEN, so both of these are allow-lists and
# anything unlisted falls through to HOLD. That asymmetry is the whole design:
# a status we don't recognise can neither grant a tier nor take one away.
ENTITLING_STATUSES = frozenset({"active", "trial"})
ENDED_STATUSES = frozenset({"expired", "paused"})
CANCELLED_STATUS = "canceled"


class EntitlementDecision(enum.Enum):
    """What a status says to do with the user's policy."""

    GRANT = "grant"
    HOLD = "hold"
    REVOKE = "revoke"


class EntitlementReason(enum.Enum):
    GRANTED = "granted"
    REVOKED = "revoked"
    HELD = "held"
    BLOCKED = "blocked"
    ADMIN_OVERRIDE = "admin_override"
    NO_REFERENCE = "no_reference"
    UNKNOWN_USER = "unknown_user"
    UNKNOWN_PLAN = "unknown_plan"
    UNKNOWN_SUBSCRIPTION = "unknown_subscription"
    REFERENCE_MISMATCH = "reference_mismatch"


@dataclass(frozen=True)
class EntitlementOutcome:
    applied: bool
    reason: EntitlementReason


@dataclass(frozen=True)
class Resolution:
    """Everything decided, before anything is written.

    Pulling the truth table out of the write path is the point: which of
    (decision, blocked, admin_held) produces which effect is one readable
    function rather than a sequence of interleaved conditionals.
    """

    write_policy: bool
    scheduling_id: int | None
    source: str | None
    granted_scheduling_id: int | None
    reason: EntitlementReason


def resolve_entitlement(
    decision: EntitlementDecision,
    *,
    blocked: bool,
    admin_held: bool,
    plan_scheduling_id: int,
    existing_granted: int | None,
) -> Resolution:
    """The whole truth table, in one place. Pure."""
    if decision is EntitlementDecision.GRANT:
        if blocked:
            return Resolution(
                write_policy=False,
                scheduling_id=None,
                source=None,
                granted_scheduling_id=existing_granted,
                reason=EntitlementReason.BLOCKED,
            )
        # Billing never erases an admin grant: the user gets what they are
        # paying for, and the comp's protection from a later lapse survives it.
        # Overwriting the source here would let the next `expired` revoke them.
        source = (
            SchedulingSource.ADMIN.value
            if admin_held
            else SchedulingSource.BILLING.value
        )
        return Resolution(
            write_policy=True,
            scheduling_id=plan_scheduling_id,
            source=source,
            granted_scheduling_id=plan_scheduling_id,
            reason=EntitlementReason.GRANTED,
        )

    if decision is EntitlementDecision.REVOKE:
        if admin_held:
            return Resolution(
                write_policy=False,
                scheduling_id=None,
                source=None,
                granted_scheduling_id=existing_granted,
                reason=EntitlementReason.ADMIN_OVERRIDE,
            )
        # Back to the default policy and out of billing's hands — the same state
        # as a user who never subscribed.
        return Resolution(
            write_policy=True,
            scheduling_id=None,
            source=SchedulingSource.DEFAULT.value,
            granted_scheduling_id=None,
            reason=EntitlementReason.REVOKED,
        )

    # HOLD carries the previous grant forward: the policy is untouched, so the
    # record of what they hold must be too.
    return Resolution(
        write_policy=False,
        scheduling_id=None,
        source=None,
        granted_scheduling_id=existing_granted,
        reason=EntitlementReason.HELD,
    )


def _is_admin_held(source: str) -> bool:
    return source == SchedulingSource.ADMIN.value


def decide_entitlement(
    flash_status: str,
    *,
    cancel_effective_date: datetime | None,
    current_period_end: datetime | None,
    now: datetime,
) -> EntitlementDecision:
    """What this status means for the user's policy. Pure.

    HOLD is the default for everything uncertain — a failed renewal Flash is
    still retrying, a cancellation whose paid period hasn't run out, a status we
    have never seen. We only REVOKE where the ending is not in doubt.
    """
    if flash_status in ENTITLING_STATUSES:
        return EntitlementDecision.GRANT
    if flash_status in ENDED_STATUSES:
        return EntitlementDecision.REVOKE
    if flash_status == CANCELLED_STATUS:
        # Flash words this in the past tense — "ended by the subscriber or by
        # you" — so a cancellation with no future end date has already happened.
        # A date is what defers it, and until then they keep what they paid for.
        ends_at = cancel_effective_date or current_period_end
        if ends_at is not None and now < ends_at:
            return EntitlementDecision.HOLD
        return EntitlementDecision.REVOKE
    return EntitlementDecision.HOLD


def _utc_now() -> datetime:
    """Naive UTC — the epoch every Flash timestamp is normalized to on the way in."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def apply_entitlement(
    db: AsyncDBSession, *, external_ref: str | None, subscription_id: str | None
) -> EntitlementOutcome:
    """Reconcile one subscriber against Flash, in a single transaction.

    The subscription record and the policy assignment commit together or not at
    all: a tier without a record, or a record without the tier, is the exact
    divergence the admin view exists to catch, so it should be impossible to
    create rather than merely detectable.
    """
    if not external_ref:
        # A plain-link signup with no reference of ours. Recorded upstream;
        # nothing here can safely be attributed to a user.
        logger.warning("Flash subscription %s carries no reference", subscription_id)
        return EntitlementOutcome(applied=False, reason=EntitlementReason.NO_REFERENCE)

    if not await brainstorm_nsec_exists_by_pubkey_on_db(db, external_ref):
        logger.warning(
            "Flash reference %s matches no user; leaving every tier alone",
            external_ref,
        )
        return EntitlementOutcome(applied=False, reason=EntitlementReason.UNKNOWN_USER)

    # Propagated, not swallowed: the caller decides what a failure means. The
    # webhook path logs and moves on (the event is recorded and replayable);
    # the reconcile loop records it against the subscriber and, for a credential
    # failure, stops rather than repeating it once per row.
    subscription = await fetch_subscription(
        subscription_id=subscription_id, ref=None if subscription_id else external_ref
    )

    if subscription is None:
        logger.warning("Flash has no subscription %s; no tier changed", subscription_id)
        return EntitlementOutcome(applied=False, reason=EntitlementReason.UNKNOWN_SUBSCRIPTION)

    if subscription.ref and subscription.ref != external_ref:
        # The event named one user and Flash's record names another. Both come
        # from Flash, so disagreement means something is wrong — and acting on
        # it would move the wrong person's tier.
        logger.error(
            "Flash subscription %s is for %s, not %s; no tier changed",
            subscription.id,
            subscription.ref,
            external_ref,
        )
        return EntitlementOutcome(
            applied=False, reason=EntitlementReason.REFERENCE_MISMATCH
        )

    plan = await get_billing_plan_on_db(
        db,
        flash_service_id=subscription.service_id,
        flash_plan_id=subscription.plan_id,
    )
    if plan is None:
        logger.warning(
            "Flash subscription %s is for an unmapped plan %s/%s; no tier changed",
            subscription.id,
            subscription.service_id,
            subscription.plan_id,
        )
        return EntitlementOutcome(applied=False, reason=EntitlementReason.UNKNOWN_PLAN)

    return await _grant_and_record(db, external_ref, subscription, plan, _utc_now())


async def _grant_and_record(
    db: AsyncDBSession,
    pubkey: str,
    subscription: FlashSubscription,
    plan: BillingPlan,
    now: datetime,
) -> EntitlementOutcome:
    # Locks the row for this transaction, so two deliveries for one subscriber
    # can't interleave into a policy and a record that disagree.
    existing = await get_user_subscription_for_update_on_db(db, pubkey)

    resolution = resolve_entitlement(
        decide_entitlement(
            subscription.status,
            cancel_effective_date=subscription.cancel_effective_date,
            current_period_end=subscription.current_period_end,
            now=now,
        ),
        blocked=await is_billing_blocked_on_db(db, pubkey),
        admin_held=_is_admin_held(await get_scheduling_source_on_db(db, pubkey)),
        plan_scheduling_id=plan.scheduling_id,
        existing_granted=existing.granted_scheduling_id if existing else None,
    )

    if resolution.write_policy and resolution.source is not None:
        if resolution.reason is EntitlementReason.GRANTED:
            await _warn_if_policy_is_inert(db, pubkey, plan.scheduling_id)
        await set_scheduling_for_pubkey_on_db(
            db, pubkey, resolution.scheduling_id, source=resolution.source
        )

    await upsert_user_subscription_on_db(
        db,
        pubkey=pubkey,
        flash_subscription_id=subscription.id,
        flash_subscriber_id=subscription.subscriber_id,
        billing_plan_id=plan.id,
        # What we actually gave, not what the plan currently says it gives —
        # retuning a plan later must not strand whoever it already granted.
        granted_scheduling_id=resolution.granted_scheduling_id,
        flash_status=subscription.status,
        current_period_start=subscription.current_period_start,
        current_period_end=subscription.current_period_end,
        next_billing_date=subscription.next_billing_date,
        trial_end_date=subscription.trial_end_date,
        cancel_effective_date=subscription.cancel_effective_date,
    )
    await db.commit()

    logger.info(
        "%s: Flash reports %s, outcome %s",
        pubkey,
        subscription.status,
        resolution.reason.value,
    )
    return EntitlementOutcome(
        applied=resolution.write_policy, reason=resolution.reason
    )


async def _warn_if_policy_is_inert(
    db: AsyncDBSession, pubkey: str, scheduling_id: int
) -> None:
    """A disabled policy will never run, so this is someone charged for nothing.

    Granted anyway: re-enabling the policy repairs everyone at once, where
    withholding would scatter paying users onto free. But it is not
    report-later news.
    """
    policy = await get_scheduling_on_db(db, scheduling_id)
    if policy is not None and not policy.enabled:
        logger.error(
            "PAYING USER ON DISABLED POLICY: %s granted scheduling policy %s, "
            "which is disabled and will never run",
            pubkey,
            scheduling_id,
        )
