from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_request_repo import (
    build_recent_brainstorm_requests_stmt,
)
from app.schemas.schemas import BrainstormRequestInstance
from app.services.brainstorm_request_service import (
    brainstorm_request_db_obj_to_schema_converter,
)

router = APIRouter()


@router.get(
    path="",
    response_model=Page[BrainstormRequestInstance],
    summary="Admin: recent brainstorm request activity (last N days, all users)",
)
async def get_activity_endpoint(
    status: Optional[str] = None,
    algorithm: Optional[str] = None,
    pubkey: Optional[str] = None,
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
            brainstorm_request_db_obj_to_schema_converter(r, include_result=False, is_admin=True)
            for r in rows
        ],
    )
