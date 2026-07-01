"""Data access for the `scheduling` table (scheduling policies / tiers).

The catalog of policies a user can be assigned to. Reads only for now; admin
CRUD (add/rename/retune rows) lands later. The interactive lanes (Admin /
Manual / House) are hardcoded elsewhere and are not rows here.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import Scheduling


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


async def scheduling_exists_on_db(db: AsyncDBSession, scheduling_id: int) -> bool:
    statement = select(Scheduling.id).where(Scheduling.id == scheduling_id)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None
