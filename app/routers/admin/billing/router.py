"""Billing visibility for whoever answers "did my payment go through?".

Mounted outside the admin router on purpose. Being on the billing list must not
confer general administration — see `app/core/billing_admin_whitelist.py`.
"""

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.billing_admin_whitelist import get_billing_pubkeys
from app.core.database import get_db
from app.core.flash import (
    FlashCredentialError,
    FlashRefused,
    FlashUnavailable,
    fetch_subscription_raw,
)
from app.core.loggr import loggr
from app.repos.user_subscription_repo import build_billing_subscriptions_stmt
from app.schemas.schemas import (
    AttributeUnresolvedBody,
    BillingBlockOutcome,
    BillingPlanItem,
    BillingSubscriptionActionOutcome,
    BillingSubscriptionItem,
    CancelSubscriptionBody,
    CreateBillingPlanBody,
    DivergenceSection,
    SetSubscriptionStatusBody,
    UnresolvedResolutionOutcome,
    UpdateBillingPlanBody,
)
from app.services.billing_service import (
    apply_entitlement,
    attribute_unresolved_subscription,
    cancel_subscriber_subscription,
    create_billing_plan,
    dismiss_unresolved_subscription,
    list_billing_plans_admin,
    set_billing_block,
    set_subscriber_subscription_status,
    update_billing_plan,
)
from app.services.billing_visibility_service import build_divergence_response
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


# Writes need the `subscriptions:manage` scope reads do not, so on a write a
# refused credential most likely means a key that can look but not act. Same
# status either way — only the sentence differs, because "Flash is down" would
# have an operator wait for something that is never coming back.
_CREDENTIAL_REFUSED = (
    "Flash refused our credentials. The API key needs attention; "
    "retrying will not help."
)
_CREDENTIAL_REFUSED_ON_WRITE = (
    "Flash refused our credentials, so nothing was changed. The API key may not "
    "carry the scope needed to manage subscriptions; retrying will not help."
)


@contextmanager
def _flash_failure_as_http(credential_detail: str = _CREDENTIAL_REFUSED):
    """The two ways asking Flash can fail, told apart.

    503 is us not having been able to ask; 502 is our credential being refused —
    reported rather than retried, since it will fail identically forever. Shared
    by every operator path that talks to Flash so an outage and a dead API key
    cannot come back as the same thing on one endpoint and not another. Only the
    502's wording varies, and only to name the scope a write needs.
    """
    try:
        yield
    except FlashRefused as declined:
        # Flash was reached and said no. 409, not 503: there is nothing to wait
        # for, and the operator can look at what Flash actually holds.
        logger.warning("Flash declined an operator action: %s", declined)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Flash declined this change ({declined.status_code}). Nothing "
            "was changed — check Flash's raw record; the subscription may "
            "already be in the state you asked for.",
        ) from declined
    except FlashCredentialError as refused:
        logger.error("Flash refused our credentials on an operator action: %s", refused)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=credential_detail
        ) from refused
    except FlashUnavailable as unreachable:
        logger.warning("Could not read Flash for an operator action: %s", unreachable)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach Flash, so we do not know what it says. "
            "Nothing was changed.",
        ) from unreachable


@router.post(
    path="/subscriptions/{pubkey}/cancel",
    response_model=BillingSubscriptionActionOutcome,
    summary="Billing: cancel one subscriber's subscription in Flash",
)
async def cancel_subscription_endpoint(
    request: Request,
    pubkey: str,
    body: CancelSubscriptionBody | None = None,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    """Read `cancellation_scheduled`, not `flash_status`: under the account's
    end-of-period policy Flash cancels successfully and still reports the
    subscriber `active` until the effective date lands."""
    jwt_data: JWTData = request.state.jwt_data
    with _flash_failure_as_http(_CREDENTIAL_REFUSED_ON_WRITE):
        outcome = await cancel_subscriber_subscription(
            db,
            pubkey=pubkey,
            reason=body.reason if body else None,
            acting_pubkey=jwt_data.nostr_pubkey,
        )
    return BillingSubscriptionActionOutcome.model_validate(outcome)


@router.patch(
    path="/subscriptions/{pubkey}/status",
    response_model=BillingSubscriptionActionOutcome,
    summary="Billing: pause one subscriber's subscription, or put it back",
)
async def set_subscription_status_endpoint(
    request: Request,
    pubkey: str,
    body: SetSubscriptionStatusBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    with _flash_failure_as_http(_CREDENTIAL_REFUSED_ON_WRITE):
        outcome = await set_subscriber_subscription_status(
            db,
            pubkey=pubkey,
            flash_status=body.status,
            acting_pubkey=jwt_data.nostr_pubkey,
        )
    return BillingSubscriptionActionOutcome.model_validate(outcome)


async def _read_flash_record(
    request: Request, *, subscription_id: str | None = None, ref: str | None = None
) -> dict:
    """Flash's own body, verbatim, for whichever handle the caller has.

    Read-only by construction: nothing here applies entitlement, so the stored
    row and the scheduling assignment are exactly as they were afterwards.

    404 is Flash saying there is no such subscription, kept apart from the two
    failures above because acting on the wrong one dismisses a real customer.
    """
    jwt_data: JWTData = request.state.jwt_data
    await validate_flash_record_read_allowed(jwt_data.nostr_pubkey)

    with _flash_failure_as_http():
        record = await fetch_subscription_raw(
            subscription_id=subscription_id, ref=ref
        )

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


def _resolution_response(outcome) -> UnresolvedResolutionOutcome:
    return UnresolvedResolutionOutcome(
        subscription_id=outcome.subscription_id,
        resolution=outcome.resolution.value,
        pubkey=outcome.pubkey,
        applied=outcome.applied,
        events_settled=outcome.events_settled,
    )


@router.post(
    path="/unresolved/{subscription_id}/attribute",
    response_model=UnresolvedResolutionOutcome,
    summary="Billing: attach a signup that named nobody to the user who made it",
)
async def attribute_unresolved_endpoint(
    request: Request,
    subscription_id: str,
    body: AttributeUnresolvedBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    with _flash_failure_as_http():
        outcome = await attribute_unresolved_subscription(
            db,
            subscription_id=subscription_id,
            pubkey=body.pubkey,
            acting_pubkey=jwt_data.nostr_pubkey,
        )
    return _resolution_response(outcome)


@router.post(
    path="/unresolved/{subscription_id}/dismiss",
    response_model=UnresolvedResolutionOutcome,
    summary="Billing: write a signup off as not a customer",
)
async def dismiss_unresolved_endpoint(
    request: Request,
    subscription_id: str,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    outcome = await dismiss_unresolved_subscription(
        db, subscription_id=subscription_id, acting_pubkey=jwt_data.nostr_pubkey
    )
    return _resolution_response(outcome)


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
    # exclude_unset, not exclude_none: an explicit null is a real edit, and
    # exclude_none would drop it silently.
    return await update_billing_plan(db, plan_id, body.model_dump(exclude_unset=True))
