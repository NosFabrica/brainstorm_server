"""Data access for the `scheduling` table (scheduling policies / tiers).

The catalog of policies a user can be assigned to. Reads only for now; admin
CRUD (add/rename/retune rows) lands later. The interactive lanes (Admin /
Manual / House) are hardcoded elsewhere and are not rows here.
"""

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import BrainstormNsec, Scheduling


async def list_scheduling_on_db(db: AsyncDBSession) -> list[Scheduling]:
    statement = select(Scheduling).order_by(Scheduling.priority.desc(), Scheduling.id)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def get_scheduling_on_db(
    db: AsyncDBSession, scheduling_id: int
) -> Scheduling | None:
    statement = select(Scheduling).where(Scheduling.id == scheduling_id)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def get_default_scheduling_on_db(db: AsyncDBSession) -> Scheduling | None:
    """The policy used for users with no explicit assignment (is_default row)."""
    statement = select(Scheduling).where(Scheduling.is_default.is_(True)).limit(1)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def select_public_scheduling_on_db(db: AsyncDBSession) -> list[Scheduling]:
    """The policies that may reach the public pricing page, default first.

    Default first because it is the one option nobody can buy, so it has a
    placement rule rather than a sort field; plans carry their own `sort_order`.
    """
    statement = (
        select(Scheduling)
        .where(Scheduling.is_public.is_(True))
        .order_by(Scheduling.is_default.desc(), Scheduling.id.asc())
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def scheduling_exists_on_db(db: AsyncDBSession, scheduling_id: int) -> bool:
    statement = select(Scheduling.id).where(Scheduling.id == scheduling_id)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None


async def create_scheduling_on_db(
    db: AsyncDBSession,
    name: str,
    schedule_interval_seconds: int,
    priority: int,
    enabled: bool,
    is_default: bool,
    manual_quota_limit: int,
    manual_quota_window_seconds: int,
    is_public: bool = False,
) -> Scheduling:
    row = Scheduling(
        name=name,
        schedule_interval_seconds=schedule_interval_seconds,
        priority=priority,
        enabled=enabled,
        is_default=is_default,
        manual_quota_limit=manual_quota_limit,
        manual_quota_window_seconds=manual_quota_window_seconds,
        is_public=is_public,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def unset_default_scheduling_on_db(db: AsyncDBSession) -> None:
    """Clear is_default on the current default (the partial-unique index allows
    only one). Call inside the same transaction before setting a new default."""
    await db.execute(
        update(Scheduling)
        .where(Scheduling.is_default.is_(True))
        .values(is_default=False)
    )


async def update_scheduling_on_db(
    db: AsyncDBSession, scheduling_id: int, **fields
) -> Scheduling | None:
    if fields:
        await db.execute(
            update(Scheduling).where(Scheduling.id == scheduling_id).values(**fields)
        )
    return await get_scheduling_on_db(db, scheduling_id)


async def count_users_on_scheduling_on_db(
    db: AsyncDBSession, scheduling_id: int
) -> int:
    statement = select(func.count()).where(
        BrainstormNsec.scheduling_id == scheduling_id
    )
    result = await execute_db_statement(db, statement, __name__)
    return int(result.scalar_one())


async def delete_scheduling_on_db(db: AsyncDBSession, scheduling_id: int) -> None:
    await db.execute(delete(Scheduling).where(Scheduling.id == scheduling_id))


def build_scheduling_users_stmt(scheduling_id: int, include_null: bool) -> Select:
    """Users assigned to a policy. For the default policy, `include_null` also
    picks up unassigned (scheduling_id IS NULL) users. Paginate this."""
    condition = BrainstormNsec.scheduling_id == scheduling_id
    if include_null:
        condition = or_(condition, BrainstormNsec.scheduling_id.is_(None))
    return select(
        BrainstormNsec.pubkey,
        BrainstormNsec.last_time_published_graperank,
    ).where(condition)
