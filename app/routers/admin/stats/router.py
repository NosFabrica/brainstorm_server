from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement, get_db
from app.db_models import BrainstormNsec
from app.repos.brainstorm_request_repo import compute_admin_stats_on_db
from app.schemas.request_response_schemas import AdminStatsResponse
from app.schemas.schemas import AdminStats, SchedulerStats
from app.services.scheduler_stats_service import get_scheduler_stats

router = APIRouter()


@router.get(
    path="/scheduler",
    response_model=SchedulerStats,
    summary="Admin: tier-scheduler self-measured stats (optional feature)",
)
async def get_scheduler_stats_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
) -> SchedulerStats:
    return await get_scheduler_stats(db)


@router.get(
    path="",
    summary="Admin: platform stats",
)
async def get_admin_stats_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminStatsResponse:
    req_stats = await compute_admin_stats_on_db(db)

    total_users_result = await execute_db_statement(
        db, select(func.count()).select_from(BrainstormNsec), __name__
    )
    total_users = total_users_result.scalar_one()

    return AdminStatsResponse(
        data=AdminStats(
            total_users=total_users,
            scored_users=req_stats["scored_users"],
            sp_adopters=req_stats["sp_adopters"],
            total_reports=None,
            queue_depth=req_stats["queue_depth"],
        )
    )