from typing import Optional

from fastapi import APIRouter, Depends
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_request_repo import (
    select_recent_active_pubkeys_on_db,
    select_recent_brainstorm_requests_on_db,
)
from app.schemas.request_response_schemas import (
    AdminHistoryResponse,
    AdminUsersListResponse,
)
from app.schemas.schemas import AdminUserListItem, AdminUsersListData
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
)

router = APIRouter()


@router.get(
    path="",
    summary="Admin: users active in last 30d (paginated)",
)
async def get_recent_users_endpoint(
    page: int = 0,
    limit: int = 25,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminUsersListResponse:
    rows, total = await select_recent_active_pubkeys_on_db(
        db, page=page, limit=limit
    )
    items: list[AdminUserListItem] = []
    for r in rows:
        nsec = r.pop("nsec", None)
        ta_pubkey = (
            Keys.parse(secret_key=nsec).public_key().to_hex() if nsec else None
        )
        items.append(AdminUserListItem(ta_pubkey=ta_pubkey, **r))
    return AdminUsersListResponse(
        data=AdminUsersListData(
            data=items,
            total=total,
            page=page,
            limit=limit,
        )
    )


@router.get(
    path="/{pubkey}/history",
    summary="Admin: graperank request history for a pubkey (last 30d)",
)
async def get_user_history_endpoint(
    pubkey: str,
    status: Optional[str] = None,
    algorithm: Optional[str] = None,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminHistoryResponse:
    rows = await select_recent_brainstorm_requests_on_db(
        db, pubkey=pubkey, status=status, algorithm=algorithm
    )
    return AdminHistoryResponse(
        data=[
            brainstorm_request_db_obj_to_schema_converter(r, include_result=False)
            for r in rows
        ]
    )
