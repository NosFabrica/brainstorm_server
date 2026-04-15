from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_request_repo import (
    select_recent_brainstorm_requests_on_db,
)
from app.schemas.request_response_schemas import AdminHistoryResponse
from app.schemas.schemas import AdminHistoryData
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
)

router = APIRouter()


@router.get(
    path="",
    summary="Admin: recent brainstorm request activity (last 30d, all users)",
)
async def get_activity_endpoint(
    status: Optional[str] = None,
    algorithm: Optional[str] = None,
    pubkey: Optional[str] = None,
    page: int = 0,
    limit: int = 25,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminHistoryResponse:
    rows, total = await select_recent_brainstorm_requests_on_db(
        db,
        pubkey=pubkey,
        status=status,
        algorithm=algorithm,
        page=page,
        limit=limit,
    )
    return AdminHistoryResponse(
        data=AdminHistoryData(
            data=[
                brainstorm_request_db_obj_to_schema_converter(r, include_result=False)
                for r in rows
            ],
            total=total,
            page=page,
            limit=limit,
        )
    )
