"""The UI-facing read side of billing: one subscriber's view, and the plans page.

Two rules, both from the PRD. `tier` derives from the scheduling assignment —
what the user actually receives — never from the billing record, so the UI is
structurally unable to claim something the scheduler isn't delivering. And
Flash's status vocabulary is translated here, on read: the client's
`normalize()` maps anything unrecognised to `active`, so a raw `expired`
passed through would render as paid.
"""

from datetime import timedelta
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.db_models import BillingPlan, Scheduling
from app.repos.billing_plan_repo import (
    get_billing_plan_by_id_on_db,
    get_plan_by_scheduling_id_on_db,
    select_billing_plans_on_db,
)
from app.repos.user_subscription_repo import AbandonRule, get_user_subscription_on_db
from app.repos.scheduling_repo import (
    get_default_scheduling_on_db,
    get_scheduling_on_db,
)
from app.repos.brainstorm_nsec import get_assigned_scheduling_id_on_db
from app.schemas.schemas import BillingPlanView, BillingPlansData, SubscriptionView
from app.services.billing_service import EntitlementReason, utc_now

FREE_TIER = "free"


def _abandon_rule() -> AbandonRule:
    """One rule, read from settings in both places that ask."""
    return AbandonRule(
        after=timedelta(seconds=settings.billing_abandon_pending_after_seconds),
        error=EntitlementReason.UNKNOWN_SUBSCRIPTION.value,
    )

# Flash subscription status → the UI's vocabulary. `past_due` reads as `grace`
# because the user is inside Flash's dunning and still entitled —
# `useSubscription.isActive` counts `grace` but not `past_due`. An unlisted
# status is answered from the policy instead (see `_translate`).
_UI_STATUS = {
    "active": "active",
    "trial": "active",
    "pending": "pending",
    "past_due": "grace",
    "paused": "canceled",
    "canceled": "canceled",
    "expired": "canceled",
}


def _translate(flash_status: str | None, *, tier: str) -> str:
    if flash_status is None:
        # No billing record. A paid policy with no record is a comp — report
        # what they hold.
        return "active" if tier != FREE_TIER else "none"
    known = _UI_STATUS.get(flash_status)
    if known is not None:
        return known
    # Unrecognised: change nothing, report what the policy already says.
    return "active" if tier != FREE_TIER else "none"


async def _resolve_tier(db: AsyncDBSession, pubkey: str) -> str:
    scheduling_id = await get_assigned_scheduling_id_on_db(db, pubkey)
    if scheduling_id is None:
        return FREE_TIER
    plan = await get_plan_by_scheduling_id_on_db(db, scheduling_id)
    return plan.subscription_tier if plan is not None else FREE_TIER


async def read_subscription_view(db: AsyncDBSession, pubkey: str) -> SubscriptionView:
    """What one signed-in user has. Every field present, always."""
    tier = await _resolve_tier(db, pubkey)
    row = await get_user_subscription_on_db(db, pubkey)

    # A checkout Flash discarded is not a payment being confirmed, it is no
    # subscription at all — and the row only survives it because the sweep needs
    # a handle. Presenting it as one keeps "confirming your payment" on screen
    # for a user whose payment will never confirm, with no way to start again.
    if row is not None and _abandon_rule().matches(row, utc_now()):
        row = None

    manage_url = None
    if row is not None and settings.flash_enabled:
        plan = await get_billing_plan_by_id_on_db(db, row.billing_plan_id)
        if plan is not None:
            base = settings.flash_base_url.rstrip("/")
            manage_url = f"{base}/subscriptions/portal/{plan.flash_service_id}"

    return SubscriptionView(
        tier=tier,
        status=_translate(row.flash_status if row else None, tier=tier),
        current_period_end=row.current_period_end if row else None,
        rail=row.rail if row else None,
        manage_url=manage_url,
    )


def checkout_redirect_url() -> str:
    if settings.flash_checkout_redirect_url:
        return settings.flash_checkout_redirect_url
    return settings.frontend_url.rstrip("/") + "/billing/return"


def _checkout_url(plan: BillingPlan) -> str:
    base = settings.flash_base_url.rstrip("/")
    redirect = quote(checkout_redirect_url(), safe="")
    return (
        f"{base}/subscriptions/signup/{plan.flash_service_id}/"
        f"{plan.flash_plan_id}?redirect_uri={redirect}"
    )


async def list_billing_plans(db: AsyncDBSession) -> BillingPlansData:
    """The pricing page. An empty list IS the "no billing here" signal.

    Cadence comes off the live `scheduling` rows, so the page cannot drift from
    what the scheduler actually does. `checkout_url` is complete except `ref`,
    which the client appends per-user.
    """
    if not settings.flash_enabled:
        return BillingPlansData(plans=[])

    plans: list[BillingPlanView] = []

    default_policy = await get_default_scheduling_on_db(db)
    if default_policy is not None:
        plans.append(
            BillingPlanView(
                tier=FREE_TIER,
                name="Free",
                amount_minor=0,
                currency="USD",
                schedule_interval_seconds=default_policy.schedule_interval_seconds,
                checkout_url=None,
            )
        )

    for plan in await select_billing_plans_on_db(db, only_active=True):
        policy: Scheduling | None = await get_scheduling_on_db(db, plan.scheduling_id)
        if policy is None:
            continue
        plans.append(
            BillingPlanView(
                tier=plan.subscription_tier,
                name=plan.subscription_tier.capitalize(),
                amount_minor=plan.amount_minor,
                currency=plan.currency,
                schedule_interval_seconds=policy.schedule_interval_seconds,
                checkout_url=_checkout_url(plan),
            )
        )

    return BillingPlansData(plans=plans)
