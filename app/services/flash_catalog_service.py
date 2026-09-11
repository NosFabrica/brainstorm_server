"""What Flash holds, for the operator choosing what to map.

Read live, not from the public plans cache: an operator who just edited a plan
in Flash is asking what it is now. The plan read refreshes the cache on its
way through, so the pricing page catches up the moment an admin looks.
"""

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.flash import FlashPlan, fetch_services
from app.core.flash_plan_cache import read_service_plans
from app.repos.billing_plan_repo import select_billing_plans_on_db
from app.schemas.schemas import (
    FlashPlanItem,
    FlashServiceItem,
    FlashServicePlansData,
    FlashServicesData,
)


async def list_flash_services() -> FlashServicesData:
    services = await fetch_services()
    return FlashServicesData(
        services=[
            FlashServiceItem(
                id=service.id,
                name=service.name,
                description=service.description,
                signup_url=service.signup_url,
            )
            for service in services
        ]
    )


async def list_flash_service_plans(
    db: AsyncDBSession, service_id: str
) -> FlashServicePlansData:
    """Raises `FlashServiceMissing` when Flash holds no such service."""
    plans = await read_service_plans(service_id, fresh=True)
    mapped = {
        (row.flash_service_id, row.flash_plan_id): row.id
        for row in await select_billing_plans_on_db(db)
    }
    return FlashServicePlansData(
        service_id=service_id,
        plans=[
            _plan_item(plan, mapped.get((plan.service_id, plan.id)))
            for plan in sorted(plans, key=lambda plan: plan.sort_order)
        ],
    )


def _plan_item(plan: FlashPlan, mapping_id: int | None) -> FlashPlanItem:
    return FlashPlanItem(
        id=plan.id,
        service_id=plan.service_id,
        name=plan.name,
        description=plan.description,
        amount_minor=plan.amount_minor,
        currency=plan.currency,
        billing_interval=plan.billing_interval,
        status=plan.status,
        sort_order=plan.sort_order,
        signup_url=plan.signup_url,
        mapping_id=mapping_id,
    )
