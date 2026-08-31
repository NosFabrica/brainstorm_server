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
from app.core.flash import (
    FlashCredentialError,
    FlashUnavailable,
    fetch_subscription_raw,
)
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
from app.utils.rate_limiting.rate_limiting import validate_flash_record_read_allowed

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


async def _read_flash_record(
    request: Request, *, subscription_id: str | None = None, ref: str | None = None
) -> dict:
    """Flash's own body, verbatim, for whichever handle the caller has.

    Read-only by construction: nothing here applies entitlement, so the stored
    row and the scheduling assignment are exactly as they were afterwards.

    The three answers are kept apart because acting on the wrong one dismisses a
    real customer: 404 is Flash saying there is no such subscription, 503 is us
    not having been able to ask, and 502 is our credential being refused —
    reported rather than retried, since it will fail identically forever.
    """
    jwt_data: JWTData = request.state.jwt_data
    await validate_flash_record_read_allowed(jwt_data.nostr_pubkey)

    try:
        record = await fetch_subscription_raw(
            subscription_id=subscription_id, ref=ref
        )
    except FlashCredentialError as refused:
        logger.error("Flash refused our credentials on an operator lookup: %s", refused)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Flash refused our credentials. The API key needs attention; "
            "retrying will not help.",
        ) from refused
    except FlashUnavailable as unreachable:
        logger.warning("Could not read Flash for an operator lookup: %s", unreachable)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach Flash, so we do not know what it says. "
            "Nothing was changed.",
        ) from unreachable

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Flash has no subscription for this record.",
        )
    return record


@router.get(
    path="/subscriptions/{pubkey}/flash",
    summary="Billing: what Flash says about one subscriber, unmodified",
)
async def read_subscriber_flash_record_endpoint(request: Request, pubkey: str):
    return await _read_flash_record(request, ref=pubkey)


@router.get(
    path="/unresolved/{subscription_id}/flash",
    summary="Billing: what Flash says about a signup we could not attribute",
)
async def read_unresolved_flash_record_endpoint(
    request: Request, subscription_id: str
):
    # An unresolved signup has no pubkey, so its Flash id is the only handle it
    # has — hence a second sub-resource rather than one two-parameter endpoint
    # nobody could call with both.
    return await _read_flash_record(request, subscription_id=subscription_id)


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
    # exclude_unset, not exclude_none: clearing a billing period or a blurb back
    # to null is a real edit, and exclude_none would drop it silently.
    return await update_billing_plan(db, plan_id, body.model_dump(exclude_unset=True))


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
