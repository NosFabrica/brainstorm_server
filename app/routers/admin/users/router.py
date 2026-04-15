from typing import Optional

from fastapi import APIRouter, Depends, Query
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
from app.schemas.schemas import AdminUserListItem, BrainstormRequestInstance
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
)

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
    days: int = Query(30, ge=1, le=365),
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
            brainstorm_request_db_obj_to_schema_converter(r, include_result=False)
            for r in rows
        ],
    )
