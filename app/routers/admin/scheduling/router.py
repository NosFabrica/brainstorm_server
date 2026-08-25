from fastapi import APIRouter, Depends, HTTPException
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_nsec import bulk_set_scheduling_for_pubkeys_on_db
from app.repos.scheduling_repo import (
    build_scheduling_users_stmt,
    count_users_on_scheduling_on_db,
    create_scheduling_on_db,
    delete_scheduling_on_db,
    get_scheduling_on_db,
    list_scheduling_on_db,
    scheduling_exists_on_db,
    unset_default_scheduling_on_db,
    update_scheduling_on_db,
)
from app.schemas.request_body_schemas import (
    BulkAssignSchedulingBody,
    CreateSchedulingBody,
    UpdateSchedulingBody,
)
from app.schemas.schemas import (
    SchedulerStats,
    SchedulingItem,
    SchedulingUserItem,
)
from app.services.scheduler_stats_service import get_scheduler_stats

router = APIRouter()


@router.get(
    path="/stats",
    response_model=SchedulerStats,
    summary="Admin: tier-scheduler self-measured stats",
)
async def get_scheduling_stats_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
) -> SchedulerStats:
    return await get_scheduler_stats(db)


@router.get(
    path="",
    response_model=list[SchedulingItem],
    summary="Admin: list scheduling policies (tiers)",
)
async def list_scheduling_endpoint(
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await list_scheduling_on_db(db)


@router.post(
    path="",
    response_model=SchedulingItem,
    status_code=201,
    summary="Admin: create a scheduling policy",
)
async def create_scheduling_endpoint(
    body: CreateSchedulingBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    if body.is_default:
        await unset_default_scheduling_on_db(db)
    return await create_scheduling_on_db(db, **body.model_dump())


@router.get(
    path="/{scheduling_id}/users",
    response_model=Page[SchedulingUserItem],
    summary="Admin: users assigned to a scheduling policy (paginated)",
)
async def list_scheduling_users_endpoint(
    scheduling_id: int,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    policy = await get_scheduling_on_db(db, scheduling_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Scheduling policy not found")
    stmt = build_scheduling_users_stmt(scheduling_id, include_null=policy.is_default)
    return await paginate(
        db,
        stmt,
        transformer=lambda rows: [
            SchedulingUserItem(
                pubkey=r.pubkey,
                last_time_published_graperank=r.last_time_published_graperank,
            )
            for r in rows
        ],
    )


@router.put(
    path="/{scheduling_id}/users",
    summary="Admin: assign many users to a scheduling policy (bulk move)",
)
async def bulk_assign_scheduling_users_endpoint(
    scheduling_id: int,
    body: BulkAssignSchedulingBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    if not await scheduling_exists_on_db(db, scheduling_id):
        raise HTTPException(status_code=404, detail="Scheduling policy not found")
    assigned = await bulk_set_scheduling_for_pubkeys_on_db(
        db, body.pubkeys, scheduling_id, source="admin"
    )
    return {"assigned": assigned}


@router.patch(
    path="/{scheduling_id}",
    response_model=SchedulingItem,
    summary="Admin: update a scheduling policy",
)
async def update_scheduling_endpoint(
    scheduling_id: int,
    body: UpdateSchedulingBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    if not await scheduling_exists_on_db(db, scheduling_id):
        raise HTTPException(status_code=404, detail="Scheduling policy not found")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("is_default"):
        await unset_default_scheduling_on_db(db)
    return await update_scheduling_on_db(db, scheduling_id, **fields)


@router.delete(
    path="/{scheduling_id}",
    status_code=204,
    summary="Admin: delete a scheduling policy",
)
async def delete_scheduling_endpoint(
    scheduling_id: int,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    policy = await get_scheduling_on_db(db, scheduling_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Scheduling policy not found")
    if policy.is_default:
        raise HTTPException(status_code=409, detail="Cannot delete the default policy")
    if await count_users_on_scheduling_on_db(db, scheduling_id) > 0:
        raise HTTPException(
            status_code=409, detail="Policy is assigned to users; reassign them first"
        )
    await delete_scheduling_on_db(db, scheduling_id)
