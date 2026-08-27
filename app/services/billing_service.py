"""Turning Flash subscription state into a scheduling policy.

Entitlement *is* the scheduling assignment; the `user_subscription` row only
records why. Two rules govern everything here: nothing is granted from an event
body (Flash's own view is read first, which also makes concurrent processing
converge), and uncertainty never costs a user their tier — an unreachable Flash,
an unrecognised status or an unmapped plan all leave the policy alone.
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.flash import FlashSubscription, fetch_subscription
from app.core.loggr import loggr
from app.db_models import BillingPlan, SchedulingSource
from fastapi import HTTPException, status

from app.repos.billing_plan_repo import (
    get_billing_plan_on_db,
    insert_billing_plan_on_db,
    select_billing_plans_on_db,
    update_billing_plan_on_db,
)
from app.repos.user_subscription_repo import (
    clear_granted_scheduling_on_db,
    get_user_subscription_on_db,
    lock_user_for_update_on_db,
    update_flash_status_on_db,
    upsert_user_subscription_on_db,
)
from app.repos.brainstorm_nsec import (
    brainstorm_nsec_exists_by_pubkey_on_db,
    get_scheduling_source_on_db,
    is_billing_blocked_on_db,
    set_billing_blocked_on_db,
    set_scheduling_for_pubkey_on_db,
)
from app.repos.scheduling_repo import get_scheduling_on_db, scheduling_exists_on_db

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
    BUSY = "busy"


# Outcomes that actually decided something. Anything else leaves the event
# unmarked so it surfaces to an operator rather than being quietly discarded.
# Shared by the live path and the replay pass so they cannot disagree.
SETTLED_REASONS = frozenset(
    {
        EntitlementReason.GRANTED,
        EntitlementReason.REVOKED,
        EntitlementReason.HELD,
        EntitlementReason.BLOCKED,
        EntitlementReason.ADMIN_OVERRIDE,
    }
)


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


def is_admin_held(source: str) -> bool:
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


def utc_now() -> datetime:
    """Naive UTC — the epoch every Flash timestamp is normalized to on the way in."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def apply_entitlement(
    db: AsyncDBSession,
    *,
    external_ref: str | None,
    subscription_id: str | None,
    yield_if_busy: bool = False,
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

    # Locked BEFORE reading Flash, not after. Fetching first and locking second
    # lets two handlers both read, then serialise on the lock — and whichever
    # read *earlier* writes last, so the older view of the subscription wins.
    # Holding the lock across the HTTP call costs a row lock for one round trip,
    # which at this volume is nothing against getting the ordering wrong.
    if not await lock_user_for_update_on_db(
        db, external_ref, skip_locked=yield_if_busy
    ):
        # Only background work asks to yield. A live webhook waits, because it
        # has ten seconds to answer and nothing else to do meanwhile.
        logger.info("%s is being reconciled elsewhere; leaving it", external_ref)
        return EntitlementOutcome(applied=False, reason=EntitlementReason.BUSY)

    # Propagated, not swallowed: the caller decides what a failure means. The
    # webhook path logs and moves on (the event is recorded and replayable);
    # the reconcile loop records it against the subscriber and, for a credential
    # failure, stops rather than repeating it once per row.
    subscription = await fetch_subscription(
        subscription_id=subscription_id, ref=None if subscription_id else external_ref
    )

    if subscription is None:
        # Named by ref, not by id: every caller here looks up by reference, so
        # interpolating the id alone identifies nobody.
        logger.warning(
            "Flash has no subscription for %s (id %s); no tier changed",
            external_ref,
            subscription_id or "not given",
        )
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

    return await _grant_and_record(db, external_ref, subscription, plan, utc_now())


async def _grant_and_record(
    db: AsyncDBSession,
    pubkey: str,
    subscription: FlashSubscription,
    plan: BillingPlan,
    now: datetime,
) -> EntitlementOutcome:
    # apply_entitlement already holds the subscriber's lock, so this is a plain
    # read — the row may not exist yet on a first subscription.
    existing = await get_user_subscription_on_db(db, pubkey)

    resolution = resolve_entitlement(
        decide_entitlement(
            subscription.status,
            cancel_effective_date=subscription.cancel_effective_date,
            current_period_end=subscription.current_period_end,
            now=now,
        ),
        blocked=await is_billing_blocked_on_db(db, pubkey),
        admin_held=is_admin_held(await get_scheduling_source_on_db(db, pubkey)),
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
        subscription=subscription,
        billing_plan_id=plan.id,
        # What we actually gave, not what the plan currently says it gives —
        # retuning a plan later must not strand whoever it already granted.
        granted_scheduling_id=resolution.granted_scheduling_id,
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


async def list_billing_plans_admin(db: AsyncDBSession) -> list[BillingPlan]:
    return await select_billing_plans_on_db(db)


async def create_billing_plan(db: AsyncDBSession, values: dict) -> BillingPlan:
    await _require_scheduling(db, values["scheduling_id"])
    plan = await insert_billing_plan_on_db(db, **values)
    await db.commit()
    return plan


async def update_billing_plan(
    db: AsyncDBSession, plan_id: int, values: dict
) -> BillingPlan:
    if "scheduling_id" in values:
        await _require_scheduling(db, values["scheduling_id"])
    plan = await update_billing_plan_on_db(db, plan_id, values)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such plan"
        )
    await db.commit()
    return plan


async def _require_scheduling(db: AsyncDBSession, scheduling_id: int) -> None:
    if not await scheduling_exists_on_db(db, scheduling_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such scheduling policy",
        )


@dataclass(frozen=True)
class BlockOutcome:
    found: bool
    blocked: bool
    revoked: bool


async def set_billing_block(
    db: AsyncDBSession, pubkey: str, *, blocked: bool
) -> BlockOutcome:
    """Bar a user from paid entitlement, or lift the bar.

    Blocking also takes back a policy billing granted — the flag alone only
    stops future grants. An admin-assigned policy is left alone, exactly as
    every other billing write leaves it. The subscription record stays: they
    are still being charged, and cancellation must never be gated on the block.
    """
    if not await brainstorm_nsec_exists_by_pubkey_on_db(db, pubkey):
        return BlockOutcome(found=False, blocked=False, revoked=False)

    await lock_user_for_update_on_db(db, pubkey)
    await set_billing_blocked_on_db(db, pubkey, blocked)

    revoked = False
    if blocked:
        source = await get_scheduling_source_on_db(db, pubkey)
        if source == SchedulingSource.BILLING.value:
            await set_scheduling_for_pubkey_on_db(
                db, pubkey, None, source=SchedulingSource.DEFAULT.value
            )
            await clear_granted_scheduling_on_db(db, pubkey)
            revoked = True

    await db.commit()
    logger.info(
        "%s billing_blocked set to %s (revoked=%s)", pubkey, blocked, revoked
    )
    return BlockOutcome(found=True, blocked=blocked, revoked=revoked)


# What a payload unambiguously implies when Flash cannot be asked. Only the
# endings: `activated`/`renewed` are excluded on purpose — their payloads omit
# the period boundaries, which are the whole reason for re-fetching.
_FALLBACK_STATUS = {
    "subscription.expired": "expired",
    "subscription.canceled": "canceled",
    "subscription.past_due": "past_due",
}


async def apply_payload_fallback(
    db: AsyncDBSession, *, event: str, external_ref: str | None
) -> bool:
    """Apply what an event implies without reading Flash. True if anything moved.

    `expired` revokes now; `canceled` and `past_due` record the status, which the
    lapse sweep and reconcile loop then act on. The event stays unprocessed, so
    the cron still replaces this with Flash's authoritative answer.
    """
    status = _FALLBACK_STATUS.get(event)
    if status is None or not external_ref:
        return False
    if not await lock_user_for_update_on_db(db, external_ref, skip_locked=True):
        return False
    existing = await get_user_subscription_on_db(db, external_ref)
    if existing is None:
        return False

    if status == "expired":
        resolution = resolve_entitlement(
            EntitlementDecision.REVOKE,
            blocked=False,
            admin_held=is_admin_held(
                await get_scheduling_source_on_db(db, external_ref)
            ),
            plan_scheduling_id=existing.granted_scheduling_id or 0,
            existing_granted=existing.granted_scheduling_id,
        )
        if resolution.write_policy and resolution.source is not None:
            await set_scheduling_for_pubkey_on_db(
                db, external_ref, resolution.scheduling_id, source=resolution.source
            )
            await clear_granted_scheduling_on_db(db, external_ref)

    # A fallback `canceled` sets the date, per the PRD: with no effective date
    # of its own, the paid-through boundary is when the entitlement ends.
    cancel_effective_date = None
    if status == "canceled" and existing.cancel_effective_date is None:
        cancel_effective_date = existing.current_period_end

    await update_flash_status_on_db(
        db, external_ref, status, cancel_effective_date=cancel_effective_date
    )
    await db.commit()
    logger.info(
        "%s: Flash unreachable, applied %s from the payload alone",
        external_ref,
        status,
    )
    return True


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
