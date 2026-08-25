"""Billing visibility for whoever answers "did my payment go through?".

Mounted outside the admin router on purpose. Being on the billing list must not
confer general administration — see `app/core/billing_admin_whitelist.py`.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.billing_admin_whitelist import get_billing_pubkeys
from app.core.database import get_db
from app.core.loggr import loggr
from app.repos.billing_repo import build_billing_subscriptions_stmt
from app.schemas.schemas import BillingSubscriptionItem, DivergenceSection
from app.services.billing_service import apply_entitlement, utc_now
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
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="flash-payments.csv"'},
    )
