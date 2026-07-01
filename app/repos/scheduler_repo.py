"""Load auto-scheduler candidates from Postgres.

One user per brainstorm_nsec row, annotated with tier priority/cadence (from the
scheduling policy, or the default) and the freshness clock. Excludes the
platform observer and anyone with a run in flight. The pure ranking/eligibility
decisions live in app/services/scheduler.py.
"""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import (
    BrainstormNsec,
    BrainstormRequest,
    BrainstormRequestStatus,
    Scheduling,
)
from app.services.scheduler import SchedulerCandidate

_NON_TERMINAL = [
    BrainstormRequestStatus.WAITING.value,
    BrainstormRequestStatus.ONGOING.value,
]


async def load_scheduler_candidates_on_db(
    db: AsyncDBSession,
    platform_pubkey: str,
    default_priority: int,
    default_interval_seconds: int,
) -> list[SchedulerCandidate]:
    last_failed_at = (
        select(func.max(BrainstormRequest.updated_at))
        .where(
            BrainstormRequest.pubkey == BrainstormNsec.pubkey,
            BrainstormRequest.status == BrainstormRequestStatus.FAILURE.value,
        )
        .correlate(BrainstormNsec)
        .scalar_subquery()
    )
    in_flight = (
        select(BrainstormRequest.private_id)
        .where(
            BrainstormRequest.pubkey == BrainstormNsec.pubkey,
            or_(
                BrainstormRequest.status.in_(_NON_TERMINAL),
                BrainstormRequest.status_ta_publication.in_(_NON_TERMINAL),
            ),
        )
        .correlate(BrainstormNsec)
        .exists()
    )
    statement = (
        select(
            BrainstormNsec.pubkey,
            func.coalesce(Scheduling.priority, default_priority).label("priority"),
            func.coalesce(
                Scheduling.schedule_interval_seconds, default_interval_seconds
            ).label("interval_seconds"),
            BrainstormNsec.last_time_published_graperank.label("last_published"),
            last_failed_at.label("last_failed_at"),
        )
        .outerjoin(Scheduling, Scheduling.id == BrainstormNsec.scheduling_id)
        .where(BrainstormNsec.pubkey != platform_pubkey, ~in_flight)
    )
    result = await execute_db_statement(db, statement, __name__)
    return [
        SchedulerCandidate(
            pubkey=row.pubkey,
            priority=int(row.priority),
            interval_seconds=int(row.interval_seconds),
            last_published=row.last_published,
            last_failed_at=row.last_failed_at,
        )
        for row in result.all()
    ]
