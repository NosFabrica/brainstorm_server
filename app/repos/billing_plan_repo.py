"""Data access for `billing_plan` — the mapping from a Flash plan to what it grants."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import BillingPlan


async def get_billing_plan_on_db(
    db: AsyncDBSession, *, flash_service_id: str, flash_plan_id: str
) -> BillingPlan | None:
    """The plan a Flash subscription belongs to. None if we don't map it.

    Deliberately NOT filtered on `is_active` — that flag means *sellable* and
    nothing else. Filtering here made retiring a plan unmap everyone on it: the
    lookup sits ahead of all status handling, so their renewals stopped landing
    *and their expiry and cancellation could never be applied*, which turned
    withdrawing a plan from sale into granting a permanent comp.
    `select_billing_plans_on_db(only_active=True)` is where the flag belongs.
    """
    statement = select(BillingPlan).where(
        BillingPlan.flash_service_id == flash_service_id,
        BillingPlan.flash_plan_id == flash_plan_id,
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def select_billing_plans_on_db(
    db: AsyncDBSession, *, only_active: bool = False
) -> list[BillingPlan]:
    """All plan mappings, oldest first. `only_active` narrows to sellable ones."""
    statement = select(BillingPlan).order_by(BillingPlan.id.asc())
    if only_active:
        statement = statement.where(BillingPlan.is_active.is_(True))
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def get_billing_plan_by_id_on_db(
    db: AsyncDBSession, plan_id: int
) -> BillingPlan | None:
    statement = select(BillingPlan).where(BillingPlan.id == plan_id)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def get_plan_by_scheduling_id_on_db(
    db: AsyncDBSession, scheduling_id: int
) -> BillingPlan | None:
    """The plan granting a policy — how a scheduling assignment maps back to a
    subscription tier name.

    Deliberately NOT filtered on `is_active`: that flag hides a plan from the
    pricing page, and a subscriber on a hidden or retired plan is still
    scheduled on what it granted — reporting them "free" would be a lie.
    Active wins only as a tiebreak when several plans grant one policy.
    """
    statement = (
        select(BillingPlan)
        .where(BillingPlan.scheduling_id == scheduling_id)
        .order_by(BillingPlan.is_active.desc(), BillingPlan.id.asc())
        .limit(1)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def insert_billing_plan_on_db(
    db: AsyncDBSession,
    *,
    flash_service_id: str,
    flash_plan_id: str,
    subscription_tier: str,
    scheduling_id: int,
    amount_minor: int,
    currency: str,
    is_active: bool,
) -> BillingPlan:
    plan = BillingPlan(
        flash_service_id=flash_service_id,
        flash_plan_id=flash_plan_id,
        subscription_tier=subscription_tier,
        scheduling_id=scheduling_id,
        amount_minor=amount_minor,
        currency=currency,
        is_active=is_active,
    )
    db.add(plan)
    await db.flush()
    return plan


async def update_billing_plan_on_db(
    db: AsyncDBSession, plan_id: int, values: dict
) -> BillingPlan | None:
    plan = await get_billing_plan_by_id_on_db(db, plan_id)
    if plan is None:
        return None
    for field, value in values.items():
        setattr(plan, field, value)
    await db.flush()
    return plan


