"""The UI-facing read side of billing: one subscriber's view, and the plans page.

Two rules, both from the PRD. What a user receives is the scheduling policy
they hold — never the billing record, so the UI is structurally unable to claim
something the scheduler isn't delivering. And Flash's status vocabulary is
translated here, on read: the client's `normalize()` maps anything unrecognised
to active, so a raw `expired` passed through would render as paid.

The policy *is* the tier. Nothing here reports a tier string, because a string
is something a client has to recognise, and one it doesn't recognise it drops.
Grouping is by policy id, paid-vs-free is `is_default`, and the billing period
is a unit and a count the client formats from rather than matches against.
"""

from datetime import timedelta
from urllib.parse import quote

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.flash import FlashPlan, FlashServiceMissing
from app.core.flash_plan_cache import read_plans_for_services
from app.core.loggr import loggr
from app.db_models import BillingPlan, Scheduling
from app.repos.billing_plan_repo import (
    get_billing_plan_by_id_on_db,
    select_billing_plans_on_db,
)
from app.repos.user_subscription_repo import AbandonRule, get_user_subscription_on_db
from app.repos.scheduling_repo import (
    get_default_scheduling_on_db,
    get_scheduling_on_db,
    select_public_scheduling_on_db,
)
from app.repos.brainstorm_nsec import get_assigned_scheduling_id_on_db
from app.schemas.schemas import (
    BillingPlanView,
    BillingPlansData,
    SubscriptionPlanView,
    SubscriptionPolicyView,
    SubscriptionView,
)
from app.services.billing_service import EntitlementReason, utc_now

logger = loggr.get_logger(__name__)

# What the free row costs when a public policy has nothing selling it. Currency
# is only a label on a zero, but the field is not nullable and every other row
# carries one.
FREE_CURRENCY = "USD"


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


def _translate(flash_status: str | None, *, is_default: bool) -> str:
    """`is_default` rather than a tier string: the default policy is the one
    nobody buys, so holding anything else is holding something paid-for. A typo
    can no longer promote a user, because there is no name to typo."""
    if flash_status is None:
        # No billing record. A non-default policy with no record is a comp —
        # report what they hold.
        return "none" if is_default else "active"
    known = _UI_STATUS.get(flash_status)
    if known is not None:
        return known
    # Unrecognised: change nothing, report what the policy already says.
    return "none" if is_default else "active"


async def _resolve_policy(db: AsyncDBSession, pubkey: str) -> Scheduling | None:
    """The policy a user holds, in one hop — their assignment, or the default.

    No tiebreak, because there is nothing to break a tie between: several plans
    may sell one policy and every one of them grants it identically.
    """
    scheduling_id = await get_assigned_scheduling_id_on_db(db, pubkey)
    if scheduling_id is not None:
        policy = await get_scheduling_on_db(db, scheduling_id)
        if policy is not None:
            return policy
    return await get_default_scheduling_on_db(db)


async def read_subscription_view(db: AsyncDBSession, pubkey: str) -> SubscriptionView:
    """What one signed-in user has. Every field present, always."""
    policy = await _resolve_policy(db, pubkey)
    row = await get_user_subscription_on_db(db, pubkey)

    # A checkout Flash discarded is not a payment being confirmed, it is no
    # subscription at all — and the row only survives it because the sweep needs
    # a handle. Presenting it as one keeps "confirming your payment" on screen
    # for a user whose payment will never confirm, with no way to start again.
    if row is not None and _abandon_rule().matches(row, utc_now()):
        row = None

    plan: BillingPlan | None = None
    if row is not None:
        plan = await get_billing_plan_by_id_on_db(db, row.billing_plan_id)

    manage_url = None
    if plan is not None and settings.flash_enabled:
        base = settings.flash_base_url.rstrip("/")
        manage_url = f"{base}/subscriptions/portal/{plan.flash_service_id}"

    return SubscriptionView(
        policy=(
            SubscriptionPolicyView.model_validate(policy) if policy is not None else None
        ),
        plan=(
            await _subscriber_plan_view(plan) if plan is not None else None
        ),
        status=_translate(
            row.flash_status if row else None,
            is_default=policy.is_default if policy is not None else True,
        ),
        current_period_start=row.current_period_start if row else None,
        current_period_end=row.current_period_end if row else None,
        next_billing_date=row.next_billing_date if row else None,
        cancel_effective_date=row.cancel_effective_date if row else None,
        manage_url=manage_url,
    )


async def _sellable_flash_plans(
    service_ids: set[str],
) -> dict[tuple[str, str], FlashPlan]:
    """Flash's plans for the services we map, minus any we could not price.

    A plan with no readable amount is withdrawn exactly like one Flash no
    longer returns: both are plans we cannot honestly put a price on, and a
    price is the one thing a pricing card cannot omit.
    """
    found = await read_plans_for_services(service_ids)
    return {key: plan for key, plan in found.items() if plan.amount_minor is not None}


async def _subscriber_plan_view(plan: BillingPlan) -> SubscriptionPlanView:
    """What they bought, priced by Flash — or unpriced, if Flash cannot be read.

    Best-effort on purpose: this call decides nothing. Their entitlement is the
    scheduling assignment, so a Flash outage costs them a price on a card, not
    a tier.
    """
    try:
        found = await read_plans_for_services({plan.flash_service_id})
    except FlashServiceMissing:
        # Loud on the pricing page, silent here: this call decides nothing, and
        # a subscriber must still see the tier they hold.
        logger.error("Flash holds no service %s", plan.flash_service_id)
        found = {}
    flash = found.get((plan.flash_service_id, plan.flash_plan_id))
    return SubscriptionPlanView(
        amount_minor=flash.amount_minor if flash else None,
        currency=flash.currency if flash else None,
        billing_interval=flash.billing_interval if flash else None,
        is_active=plan.is_active,
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


def _free_row(policy: Scheduling) -> BillingPlanView:
    """A public policy nothing sells. Not synthesized copy — the row is the
    policy, priced at zero, with no checkout because there is nothing to buy.

    No Flash plan sits behind it, so it has no plan name, no cadence and no
    feature list; the policy's own name is the only name it has ever had.
    """
    return BillingPlanView(
        policy_id=policy.id,
        policy_name=policy.name,
        schedule_interval_seconds=policy.schedule_interval_seconds,
        is_default=policy.is_default,
        plan_name=None,
        description=None,
        amount_minor=0,
        currency=FREE_CURRENCY,
        billing_interval=None,
        checkout_url=None,
        features=None,
        not_included=None,
    )


def _plan_row(
    plan: BillingPlan, policy: Scheduling, flash: FlashPlan
) -> BillingPlanView:
    return BillingPlanView(
        policy_id=policy.id,
        policy_name=policy.name,
        schedule_interval_seconds=policy.schedule_interval_seconds,
        is_default=policy.is_default,
        plan_name=flash.name,
        description=flash.description,
        amount_minor=flash.amount_minor,
        currency=flash.currency,
        billing_interval=flash.billing_interval,
        checkout_url=_checkout_url(plan),
        features=flash.features,
        not_included=flash.not_included,
    )


async def list_billing_plans(db: AsyncDBSession) -> BillingPlansData:
    """The pricing picker. An empty list IS the "no billing here" signal.

    One row per public policy nothing sells, plus one per active plan on a
    public policy that Flash still offers — so two plans on one policy are two
    rows, which is the point. Cadence comes off the live `scheduling` rows, so
    the page cannot drift from what the scheduler actually does; everything
    else comes off Flash's plan, so it cannot drift from what they charge.
    `checkout_url` is complete except `ref`, which the client appends per-user.

    A mapping Flash no longer returns cannot be priced, so it cannot be sold —
    it drops out rather than rendering blank or taking the page down.

    Order is the answer: the default policy first, because it is the one option
    nobody can buy, then plans by Flash's `sortOrder`. The client renders the
    array as given and never sorts.
    """
    if not settings.flash_enabled:
        return BillingPlansData(plans=[])

    policies = {p.id: p for p in await select_public_scheduling_on_db(db)}

    active_plans = [
        plan
        for plan in await select_billing_plans_on_db(db, only_active=True)
        if plan.scheduling_id in policies
    ]
    try:
        flash_plans = await _sellable_flash_plans(
            {plan.flash_service_id for plan in active_plans}
        )
    except FlashServiceMissing as missing:
        # Not served as an empty list. That array is a defined signal — this
        # instance sells nothing — and the UI hides every billing entry point
        # on it, so a mistyped service id would read as a deliberate
        # self-host. Refusing is what makes the fault findable; the client
        # treats an error as "unknown" and leaves the entry points alone.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Flash holds no service {missing.service_id}. The plans mapped "
                "to it cannot be sold until that id is corrected."
            ),
        ) from missing

    sellable = [
        (plan, flash_plans[(plan.flash_service_id, plan.flash_plan_id)])
        for plan in active_plans
        if (plan.flash_service_id, plan.flash_plan_id) in flash_plans
    ]
    sellable.sort(key=lambda pair: (pair[1].sort_order, pair[1].id))
    sold_policy_ids = {plan.scheduling_id for plan, _ in sellable}

    plans = [
        _free_row(policy)
        for policy in policies.values()
        if policy.id not in sold_policy_ids
    ]
    plans.extend(
        _plan_row(plan, policies[plan.scheduling_id], flash) for plan, flash in sellable
    )

    # Stable, so it only lifts the default policy and leaves everything else in
    # the order Flash's own ordering already put it in.
    plans.sort(key=lambda row: not row.is_default)
    return BillingPlansData(plans=plans)
