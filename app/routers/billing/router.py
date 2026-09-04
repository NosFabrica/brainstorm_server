"""Public billing reads. Always mounted — an empty plans list is itself the
signal that an instance has no billing, which is how the UI hides every entry
point without an env var."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_response_schemas import GetBillingPlansResponse
from app.services.subscription_view_service import list_billing_plans

router = APIRouter()


@router.get(
    path="/plans",
    summary="What's on offer, with live cadence numbers",
)
async def list_billing_plans_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetBillingPlansResponse:
    return GetBillingPlansResponse(data=await list_billing_plans(db))
