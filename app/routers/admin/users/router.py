from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_request_repo import (
    build_recent_active_pubkeys_stmt,
    build_recent_brainstorm_requests_stmt,
)
from app.schemas.admin_sort import SortOrder, UsersSort
from app.schemas.request_response_schemas import BrainstormRequestResponse
from app.schemas.schemas import AdminUserListItem, BrainstormRequestInstance
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
    create_brainstorm_request,
)
from app.services.publish_drift import resync_target_to_flags
from app.services.reconcile_service import reconcile_observer

router = APIRouter()


def _row_to_user_item(row) -> AdminUserListItem:
    d = dict(row._mapping)
    nsec = d.pop("nsec", None)
    ta_pubkey = Keys.parse(secret_key=nsec).public_key().to_hex() if nsec else None
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
    return await paginate(
        db,
        stmt,
        transformer=lambda rows: [_row_to_user_item(r) for r in rows],
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
            brainstorm_request_db_obj_to_schema_converter(
                r, include_result=False, is_admin=True
            )
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
    )
    return BrainstormRequestResponse(data=result)


@router.post(
    path="/{pubkey}/reconcile",
    summary="Admin: diff actual published state vs Neo4j desired; repair only deltas",
)
async def reconcile_observer_endpoint(
    pubkey: str,
    target: str = Query("both", description="Sink(s) to reconcile: relay|vespa|both"),
    apply: bool = Query(False, description="false = report only; true = repair deltas"),
    full: bool = Query(
        False, description="true = list every mismatch, not just first 100"
    ),
):
    # On-demand diagnosis (apply=false) + surgical repair (apply=true) for one
    # observer. Streams actual vs the live Neo4j desired state and corrects only
    # the drift — not a full re-push.
    if target not in ("relay", "vespa", "both"):
        raise HTTPException(
            status_code=422,
            detail=f"invalid target {target!r}; expected relay|vespa|both",
        )
    return await reconcile_observer(
        observer=pubkey, target=target, apply=apply, full=full
    )
