"""Ledger queries backing the scheduler's self-measured capacity metrics.

The pure math lives in app/services/scheduler_metrics.py; this only fetches the
raw numbers.
"""

from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import (
    BrainstormNsec,
    BrainstormRequest,
    BrainstormRequestStatus,
    Scheduling,
)


async def count_published_successes_since_on_db(
    db: AsyncDBSession, since: datetime
) -> int:
    """Realized throughput input: TA publishes that succeeded in the window."""
    statement = select(func.count()).where(
        BrainstormRequest.status_ta_publication
        == BrainstormRequestStatus.SUCCESS.value,
        BrainstormRequest.updated_at >= since,
    )
    result = await execute_db_statement(db, statement, __name__)
    return int(result.scalar_one())


async def publish_durations_since_on_db(
    db: AsyncDBSession, since: datetime
) -> list[float]:
    """Recent measured publish durations (for the median)."""
    statement = select(BrainstormRequest.publish_duration_seconds).where(
        BrainstormRequest.publish_duration_seconds.is_not(None),
        BrainstormRequest.updated_at >= since,
    )
    result = await execute_db_statement(db, statement, __name__)
    return [float(x) for x in result.scalars().all()]


async def tier_users_on_db(db: AsyncDBSession):
    """Per user: (tier_name, cadence_seconds, pubkey, last_published). Empty
    tiers yield one row with pubkey=None. NULL-scheduling users map to the
    default policy. Lets the caller compute demand (count) and slip (oldest
    among schedulable users) with a follows filter."""
    statement = select(
        Scheduling.name,
        Scheduling.schedule_interval_seconds,
        BrainstormNsec.pubkey,
        BrainstormNsec.last_time_published_graperank,
    ).outerjoin(
        BrainstormNsec,
        or_(
            BrainstormNsec.scheduling_id == Scheduling.id,
            and_(
                BrainstormNsec.scheduling_id.is_(None),
                Scheduling.is_default.is_(True),
            ),
        ),
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.all()


async def scheduling_priorities_on_db(db: AsyncDBSession) -> list[int]:
    """Distinct priorities configured in the scheduling table (for lane names)."""
    statement = select(Scheduling.priority).distinct()
    result = await execute_db_statement(db, statement, __name__)
    return sorted({int(p) for p in result.scalars().all()}, reverse=True)
