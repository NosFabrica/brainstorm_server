"""Turning Flash subscription state into a scheduling policy.

Entitlement *is* the scheduling assignment; the `user_subscription` row only
records why. Two rules govern everything here: nothing is granted from an event
body (Flash's own view is read first, which also makes concurrent processing
converge), and uncertainty never costs a user their tier — an unreachable Flash,
an unrecognised status or an unmapped plan all leave the policy alone.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.flash import FlashSubscription, FlashUnavailable, fetch_subscription
from app.core.loggr import loggr
from app.db_models import BillingPlan, SchedulingSource
from app.repos.billing_repo import (
    get_billing_plan_on_db,
    get_user_subscription_for_update_on_db,
    upsert_user_subscription_on_db,
)
from app.repos.brainstorm_nsec import (
    brainstorm_nsec_exists_by_pubkey_on_db,
    is_billing_blocked_on_db,
    set_scheduling_for_pubkey_on_db,
)
from app.repos.scheduling_repo import get_scheduling_on_db

logger = loggr.get_logger(__name__)

# An allow-list, so an unrecognised status never grants. Revocation is slice 03.
ENTITLING_STATUSES = frozenset({"active", "trial"})


@dataclass(frozen=True)
class EntitlementOutcome:
    applied: bool
    reason: str


def grants_entitlement(flash_status: str) -> bool:
    return flash_status in ENTITLING_STATUSES


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
        return EntitlementOutcome(applied=False, reason="no_reference")

    if not await brainstorm_nsec_exists_by_pubkey_on_db(db, external_ref):
        logger.warning(
            "Flash reference %s matches no user; leaving every tier alone",
            external_ref,
        )
        return EntitlementOutcome(applied=False, reason="unknown_user")

    try:
        subscription = await fetch_subscription(subscription_id=subscription_id)
    except FlashUnavailable as unavailable:
        logger.warning(
            "Could not read Flash for %s (%s); no tier changed", external_ref, unavailable
        )
        return EntitlementOutcome(applied=False, reason="sync_failed")

    if subscription is None:
        logger.warning("Flash has no subscription %s; no tier changed", subscription_id)
        return EntitlementOutcome(applied=False, reason="unknown_subscription")

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
        return EntitlementOutcome(applied=False, reason="unknown_plan")

    return await _grant_and_record(db, external_ref, subscription, plan)


async def _grant_and_record(
    db: AsyncDBSession,
    pubkey: str,
    subscription: FlashSubscription,
    plan: BillingPlan,
) -> EntitlementOutcome:
    # Locks the row for this transaction, so two deliveries for one subscriber
    # can't interleave into a policy and a record that disagree.
    existing = await get_user_subscription_for_update_on_db(db, pubkey)

    entitled = grants_entitlement(subscription.status)
    # Blocking is the only thing that withholds a tier from someone paying for
    # it. An admin assignment does NOT: it stops billing taking a tier away
    # (slice 03), never stops someone receiving what they are charged for.
    blocked = await is_billing_blocked_on_db(db, pubkey)
    granting = entitled and not blocked

    # When we aren't granting, carry the previous grant forward rather than
    # blanking it: a `past_due` or unrecognised status must leave the user's
    # policy alone, and slice 03 needs to know what there is to take back.
    granted_scheduling_id = (
        plan.scheduling_id
        if granting
        else (existing.granted_scheduling_id if existing else None)
    )

    if granting:
        policy = await get_scheduling_on_db(db, plan.scheduling_id)
        if policy is not None and not policy.enabled:
            # They bought a tier that will never run. Grant it anyway — the fix
            # is re-enabling the policy, which repairs everyone at once, whereas
            # withholding would scatter paying users onto free. But this is
            # someone being charged for nothing, so it is not report-later news.
            logger.error(
                "PAYING USER ON DISABLED POLICY: %s granted scheduling policy %s, "
                "which is disabled and will never run",
                pubkey,
                plan.scheduling_id,
            )
        await set_scheduling_for_pubkey_on_db(
            db, pubkey, granted_scheduling_id, source=SchedulingSource.BILLING.value
        )

    await upsert_user_subscription_on_db(
        db,
        pubkey=pubkey,
        flash_subscription_id=subscription.id,
        flash_subscriber_id=subscription.subscriber_id,
        billing_plan_id=plan.id,
        # What we actually gave, not what the plan currently says it gives —
        # retuning a plan later must not strand whoever it already granted.
        granted_scheduling_id=granted_scheduling_id,
        flash_status=subscription.status,
        current_period_start=subscription.current_period_start,
        current_period_end=subscription.current_period_end,
        next_billing_date=subscription.next_billing_date,
        trial_end_date=subscription.trial_end_date,
        cancel_effective_date=subscription.cancel_effective_date,
    )
    await db.commit()

    if blocked:
        logger.warning(
            "%s is blocked from paid entitlement; subscription recorded, nothing granted",
            pubkey,
        )
        return EntitlementOutcome(applied=False, reason="blocked")
    if not entitled:
        logger.info(
            "Flash reports %s as %s; recorded, no tier granted",
            pubkey,
            subscription.status,
        )
        return EntitlementOutcome(applied=False, reason="not_entitled")

    logger.info("%s granted scheduling policy %s", pubkey, granted_scheduling_id)
    return EntitlementOutcome(applied=True, reason="granted")
