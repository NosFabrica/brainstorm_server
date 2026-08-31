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

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
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
            SubscriptionPlanView.model_validate(plan) if plan is not None else None
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
    policy, priced at zero, with no checkout because there is nothing to buy."""
    return BillingPlanView(
        policy_id=policy.id,
        policy_name=policy.name,
        schedule_interval_seconds=policy.schedule_interval_seconds,
        is_default=policy.is_default,
        billing_period_unit=None,
        billing_period_count=None,
        amount_minor=0,
        currency=FREE_CURRENCY,
        checkout_url=None,
        blurb=None,
        includes=None,
        excludes=None,
    )


def _plan_row(plan: BillingPlan, policy: Scheduling) -> BillingPlanView:
    return BillingPlanView(
        policy_id=policy.id,
        policy_name=policy.name,
        schedule_interval_seconds=policy.schedule_interval_seconds,
        is_default=policy.is_default,
        billing_period_unit=plan.billing_period_unit,
        billing_period_count=plan.billing_period_count,
        amount_minor=plan.amount_minor,
        currency=plan.currency,
        checkout_url=_checkout_url(plan),
        blurb=plan.blurb,
        includes=plan.includes,
        excludes=plan.excludes,
    )


async def list_billing_plans(db: AsyncDBSession) -> BillingPlansData:
    """The pricing picker. An empty list IS the "no billing here" signal.

    One row per public policy nothing sells, plus one per active plan on a
    public policy — so two plans on one policy are two rows, which is the point.
    Cadence comes off the live `scheduling` rows, so the page cannot drift from
    what the scheduler actually does. `checkout_url` is complete except `ref`,
    which the client appends per-user.

    Order is the answer: the default policy first, because it is the one option
    nobody can buy, then plans by `sort_order` and id. The client renders the
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
    sold_policy_ids = {plan.scheduling_id for plan in active_plans}

    plans = [
        _free_row(policy)
        for policy in policies.values()
        if policy.id not in sold_policy_ids
    ]
    plans.extend(_plan_row(plan, policies[plan.scheduling_id]) for plan in active_plans)

    # Stable, so it only lifts the default policy and leaves everything else in
    # the order the repos already put it in.
    plans.sort(key=lambda row: not row.is_default)
    return BillingPlansData(plans=plans)
