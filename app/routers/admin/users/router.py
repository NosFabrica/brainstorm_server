from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.database import get_db
from app.core.flash import FlashCredentialError, FlashUnavailable
from app.core.loggr import loggr
from app.db_models import SchedulingSource, TriggerSource
from app.repos.brainstorm_nsec import (
    get_scheduling_for_pubkey_on_db,
    set_scheduling_for_pubkey_on_db,
)
from app.repos.brainstorm_request_repo import (
    build_recent_active_pubkeys_stmt,
    build_recent_brainstorm_requests_stmt,
)
from app.repos.scheduling_repo import (
    get_default_scheduling_on_db,
    get_scheduling_on_db,
    scheduling_exists_on_db,
)
from app.schemas.admin_sort import SortOrder, UsersSort
from app.schemas.request_body_schemas import SetUserSchedulingBody
from app.schemas.request_response_schemas import BrainstormRequestResponse
from app.schemas.schemas import (
    AdminUserDetail,
    AdminUserListItem,
    BrainstormRequestInstance,
)
from app.services.billing_service import apply_entitlement
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
    create_brainstorm_request,
)
from app.services.publish_drift import resync_target_to_flags

logger = loggr.get_logger(__name__)

router = APIRouter()


def _row_to_user_item(row, default_scheduling_name: str) -> AdminUserListItem:
    d = dict(row._mapping)
    nsec = d.pop("nsec", None)
    ta_pubkey = Keys.parse(secret_key=nsec).public_key().to_hex() if nsec else None
    # NULL scheduling_id = the user is on the default policy; show its name.
    if d.get("scheduling_name") is None:
        d["scheduling_name"] = default_scheduling_name
    return AdminUserListItem(ta_pubkey=ta_pubkey, **d)


@router.get(
    path="",
    response_model=Page[AdminUserListItem],
    summary="Admin: users active in last N days (paginated)",
)
async def get_recent_users_endpoint(
    search: Optional[str] = None,
    sort: UsersSort = UsersSort.last_triggered,
    order: SortOrder = SortOrder.desc,
    days: int = Query(30, ge=1),
    db: AsyncDBSession = Depends(dependency=get_db),
):
    stmt = build_recent_active_pubkeys_stmt(
        days=days, search=search, sort=sort, order=order
    )
    default = await get_default_scheduling_on_db(db)
    default_name = default.name if default else ""
    return await paginate(
        db,
        stmt,
        transformer=lambda rows: [_row_to_user_item(r, default_name) for r in rows],
    )


@router.get(
    path="/{pubkey}",
    response_model=AdminUserDetail,
    summary="Admin: per-user detail (effective scheduling policy, ...)",
)
async def get_user_detail_endpoint(
    pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    scheduling = await get_scheduling_for_pubkey_on_db(db, pubkey)
    return AdminUserDetail(
        pubkey=pubkey,
        scheduling_id=scheduling.id if scheduling else None,
        scheduling_name=scheduling.name if scheduling else "",
    )


@router.put(
    path="/{pubkey}/scheduling",
    response_model=AdminUserDetail,
    summary="Admin: assign a user to a scheduling policy",
)
async def set_user_scheduling_endpoint(
    pubkey: str,
    body: SetUserSchedulingBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    if not await scheduling_exists_on_db(db, body.scheduling_id):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scheduling policy id {body.scheduling_id}",
        )
    await set_scheduling_for_pubkey_on_db(
        db, pubkey, body.scheduling_id, source=SchedulingSource.ADMIN.value
    )
    scheduling = await get_scheduling_on_db(db, body.scheduling_id)
    return AdminUserDetail(
        pubkey=pubkey,
        scheduling_id=body.scheduling_id,
        scheduling_name=scheduling.name if scheduling else "",
    )


@router.delete(
    path="/{pubkey}/scheduling/override",
    response_model=AdminUserDetail,
    summary="Admin: drop the override and return a user to the default policy",
)
async def clear_user_scheduling_override_endpoint(
    pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    """Undo an admin assignment rather than replace it with another one.

    Assigning always records `admin`. Billing keeps that source even when it
    grants, and refuses to revoke against it — so a comped user who later stops
    paying keeps the tier forever, and moving them to the free policy does not
    stick while they are still paying. Setting a policy cannot say "no opinion".

    Hands them straight back to billing rather than leaving the gap: clearing
    alone would drop a paying subscriber to the default policy until the next
    sweep, up to six hours later. The re-read is best-effort — an unreachable
    Flash leaves the release standing and the sweep settles it.

    Returns the policy in effect afterwards, which is the paid one again if they
    are paying and the default if they are not.
    """
    await set_scheduling_for_pubkey_on_db(
        db, pubkey, None, source=SchedulingSource.DEFAULT.value
    )
    if settings.flash_enabled:
        try:
            await apply_entitlement(db, external_ref=pubkey, subscription_id=None)
        except (FlashUnavailable, FlashCredentialError):
            logger.warning(
                "Released %s but could not re-read Flash; the sweep will settle it",
                pubkey,
            )
    scheduling = await get_scheduling_for_pubkey_on_db(db, pubkey)
    return AdminUserDetail(
        pubkey=pubkey,
        scheduling_id=scheduling.id if scheduling else None,
        scheduling_name=scheduling.name if scheduling else "",
    )


@router.get(
    path="/{pubkey}/history",
    response_model=Page[BrainstormRequestInstance],
    summary="Admin: graperank request history for a pubkey (last N days)",
)
async def get_user_history_endpoint(
    pubkey: str,
    status: Optional[str] = None,
    algorithm: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    db: AsyncDBSession = Depends(dependency=get_db),
):
    stmt = build_recent_brainstorm_requests_stmt(
        pubkey=pubkey, status=status, algorithm=algorithm, days=days
    )
    return await paginate(
        db,
        stmt,
        transformer=lambda rows: [
            brainstorm_request_db_obj_to_schema_converter(r, is_admin=True)
            for r in rows
        ],
    )


@router.post(
    path="/{pubkey}/resync",
    response_model=BrainstormRequestResponse,
    summary="Admin: force a full re-assert (resync) of one observer's published state",
)
async def resync_observer_endpoint(
    pubkey: str,
    target: str = Query(
        "both", description="Which sink(s) to force full: relay|vespa|both"
    ),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> BrainstormRequestResponse:
    # Enqueue a normal single-observer recompute with the matching force_full_*
    # set. Never fans out — it's one request for this pubkey; the consumer reads
    # the flags off the row and re-asserts that sink's full above-cutoff state.
    try:
        force_full_relay, force_full_vespa = resync_target_to_flags(target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    result = await create_brainstorm_request(
        db=db,
        algorithm="graperank",
        parameters=pubkey,
        pubkey=pubkey,
        force_full_relay=force_full_relay,
        force_full_vespa=force_full_vespa,
        trigger_source=TriggerSource.ADMIN.value,
    )
    return BrainstormRequestResponse(data=result)
