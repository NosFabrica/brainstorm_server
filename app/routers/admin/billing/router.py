"""Billing visibility for whoever answers "did my payment go through?".

Mounted outside the admin router on purpose. Being on the billing list must not
confer general administration — see `app/core/billing_admin_whitelist.py`.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.billing_admin_whitelist import get_billing_pubkeys
from app.core.database import get_db
from app.core.loggr import loggr
from app.repos.user_subscription_repo import build_billing_subscriptions_stmt
from app.schemas.schemas import (
    BillingBlockOutcome,
    BillingPlanItem,
    BillingSubscriptionItem,
    CreateBillingPlanBody,
    DivergenceSection,
    UpdateBillingPlanBody,
)
from app.services.billing_service import (
    apply_entitlement,
    create_billing_plan,
    list_billing_plans_admin,
    set_billing_block,
    update_billing_plan,
    utc_now,
)
from app.services.billing_visibility_service import (
    build_divergence_response,
    build_payment_history_csv,
)
from app.utils.auth.auth_models import JWTData

logger = loggr.get_logger(__name__)


async def verify_billing_access(request: Request) -> None:
    """Deliberately not coupled to `admin_enabled`: that switch governs general
    administration, and turning it off should not blind whoever handles payment
    questions. Whether this surface exists at all is decided by `flash_enabled`,
    where the rest of the payment surface is decided."""
    whitelist = get_billing_pubkeys()
    if not whitelist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No billing pubkeys configured",
        )
    jwt_data: JWTData = request.state.jwt_data
    if jwt_data.nostr_pubkey not in whitelist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access billing routes",
        )


router = APIRouter()


@router.get(
    path="/subscriptions",
    response_model=Page[BillingSubscriptionItem],
    summary="Billing: subscribers, with what Flash says beside what they receive",
)
async def list_billing_subscriptions_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await paginate(db, build_billing_subscriptions_stmt())


@router.get(
    path="/divergence",
    response_model=dict[str, DivergenceSection],
    summary="Billing: everything nobody has settled",
)
async def billing_divergence_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await build_divergence_response(db)


@router.post(
    path="/subscriptions/{pubkey}/resync",
    summary="Billing: re-read one subscriber from Flash now",
)
async def resync_subscription_endpoint(
    pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    outcome = await apply_entitlement(db, external_ref=pubkey, subscription_id=None)
    logger.info("Operator forced a resync of %s: %s", pubkey, outcome.reason.value)
    return {"applied": outcome.applied, "reason": outcome.reason.value}


@router.post(
    path="/subscriptions/{pubkey}/block",
    response_model=BillingBlockOutcome,
    summary="Billing: bar a user from paid entitlement",
)
async def block_subscription_endpoint(
    pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    outcome = await set_billing_block(db, pubkey, blocked=True)
    if not outcome.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    return BillingBlockOutcome(pubkey=pubkey, blocked=True, revoked=outcome.revoked)


@router.delete(
    path="/subscriptions/{pubkey}/block",
    response_model=BillingBlockOutcome,
    summary="Billing: lift the bar; the next event or resync re-grants",
)
async def unblock_subscription_endpoint(
    pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    outcome = await set_billing_block(db, pubkey, blocked=False)
    if not outcome.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user")
    return BillingBlockOutcome(pubkey=pubkey, blocked=False, revoked=False)


@router.get(
    path="/plans",
    response_model=list[BillingPlanItem],
    summary="Billing: every plan mapping, active or not",
)
async def list_billing_plans_admin_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await list_billing_plans_admin(db)


@router.post(
    path="/plans",
    response_model=BillingPlanItem,
    status_code=status.HTTP_201_CREATED,
    summary="Billing: map a Flash plan to what it grants",
)
async def create_billing_plan_endpoint(
    body: CreateBillingPlanBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await create_billing_plan(db, body.model_dump())


@router.patch(
    path="/plans/{plan_id}",
    response_model=BillingPlanItem,
    summary="Billing: retune a plan mapping",
)
async def update_billing_plan_endpoint(
    plan_id: int,
    body: UpdateBillingPlanBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await update_billing_plan(db, plan_id, body.model_dump(exclude_none=True))


@router.get(
    path="/export.csv",
    summary="Billing: payment history for accounting",
)
async def export_payment_history_endpoint(
    since: datetime | None = Query(default=None, description="ISO 8601; defaults to 90 days ago"),
    until: datetime | None = Query(default=None, description="ISO 8601; defaults to now"),
    limit: int = Query(default=10_000, ge=1, le=50_000),
    db: AsyncDBSession = Depends(dependency=get_db),
):
    # Bounded by default: accounting wants a period, and an unbounded export
    # grows without limit and is read by pasting a URL into a browser.
    now = utc_now()
    csv_text = await build_payment_history_csv(
        db,
        since=since or now - timedelta(days=90),
        until=until or now,
        limit=limit,
    )
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="flash-payments.csv"'},
    )
