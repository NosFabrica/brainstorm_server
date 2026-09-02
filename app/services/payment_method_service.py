"""How a subscription is paid for — Lightning or card — where that is knowable.

Flash publishes no payment method on a subscription: `paymentInstrumentId` is
documented as deliberately withheld, and no webhook delivery carries one
either. What it does publish is the set of acceptance methods each PLAN takes,
and a token map for reading them (`GET /settings`).

So the answer exists for a plan that accepts exactly ONE method — a subscriber
on it cannot have paid any other way — and does not exist for a plan that
accepts several. The second case is the reason this module returns a sparse map
rather than a value per subscription: an absent key is "we cannot say", and
every surface renders that as nothing at all. There is no default and no
first-of-the-list, because a payment method guessed on a billing page is worse
than no payment method.

The narrowing case is the known limit: a plan that took cards and was later
edited to take only Lightning will report Lightning for someone who paid by
card. Nothing Flash publishes can distinguish that, and it costs an operator
action on our own account to reach.
"""

from collections.abc import Iterable, Sequence

from app.core.flash import FlashPlan
from app.core.flash_plan_cache import read_plans_for_services
from app.core.flash_settings_cache import read_acceptance_methods
from app.schemas.schemas import BillingSubscriptionItem


async def read_payment_methods(
    plans: Iterable[FlashPlan],
) -> dict[tuple[str, str], str]:
    """Keyed as a plan mapping names one: `(service_id, plan_id)`.

    A plan is present only when it accepts exactly one method AND that method
    is one Flash's settings still name. Everything else is absent.
    """
    unambiguous = [plan for plan in plans if len(plan.acceptance_methods) == 1]
    if not unambiguous:
        return {}

    methods = await read_acceptance_methods()
    resolved: dict[tuple[str, str], str] = {}
    for plan in unambiguous:
        paid_by = methods.get(plan.acceptance_methods[0])
        if paid_by:
            resolved[(plan.service_id, plan.id)] = paid_by
    return resolved


async def attach_payment_methods(rows: Sequence[BillingSubscriptionItem]) -> None:
    """Fill in how each row pays, in one read per service rather than per row.

    Written onto the rows because the roster is a paginated statement: the
    answer needs Flash, which no SQL join reaches, so it arrives after the page
    is built. A row left untouched keeps its null, and the column renders empty.
    """
    services = {row.flash_service_id for row in rows if row.flash_service_id}
    if not services:
        return

    plans = await read_plans_for_services(services)
    methods = await read_payment_methods(plans.values())
    for row in rows:
        row.payment_method = methods.get((row.flash_service_id, row.flash_plan_id))
