from datetime import datetime, timedelta

from nostr_sdk import PublicKey
from sqlalchemy import Select, and_, asc, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement, handle_no_data
from app.core.loggr import loggr
from app.db_models import (
    BrainstormNsec,
    BrainstormRequest,
    BrainstormRequestStatus,
    Scheduling,
    TriggerSource,
)
from app.schemas.admin_sort import SortOrder, UsersSort

logger = loggr.get_logger(__name__)


async def delete_brainstorm_request_by_id_on_db(
    db: AsyncDBSession, brainstorm_request_id: int
) -> BrainstormRequest:
    statement = delete(BrainstormRequest).where(
        BrainstormRequest.private_id == brainstorm_request_id
    )
    result = await execute_db_statement(db, statement, __name__)

    handle_no_data(result.rowcount)

    return result


async def select_brainstorm_request_by_id_on_db(
    db: AsyncDBSession,
    brainstorm_request_id: int,
) -> BrainstormRequest:
    statement = select(BrainstormRequest).where(
        BrainstormRequest.private_id == brainstorm_request_id,
    )
    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormRequest | None = existing_data.scalars().first()

    handle_no_data(result)
    assert result

    return result


async def count_brainstorm_requests_with_priority_over_one_on_db(
    db: AsyncDBSession,
    reference_id: int,
) -> int:
    statement = select(func.max(BrainstormRequest.private_id)).where(
        BrainstormRequest.status != BrainstormRequestStatus.WAITING.value
    )

    result = await db.execute(statement)
    last_non_waiting_id = result.scalar_one_or_none()

    if last_non_waiting_id is None:
        return reference_id

    if last_non_waiting_id > reference_id:
        return 0

    return reference_id - last_non_waiting_id


async def select_latest_brainstorm_request_on_db(
    db: AsyncDBSession, pubkey: str
) -> BrainstormRequest | None:
    statement = (
        select(BrainstormRequest)
        .where(BrainstormRequest.pubkey == pubkey)
        .order_by(desc(BrainstormRequest.created_at))
        .limit(1)
    )

    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormRequest | None = existing_data.scalars().first()

    return result


async def compute_admin_stats_on_db(db: AsyncDBSession) -> dict:
    # Queue depth = WAITING requests newer than the most recent non-WAITING one.
    # Older WAITING rows can be stuck/abandoned and are excluded intentionally.
    max_non_waiting_stmt = select(func.max(BrainstormRequest.private_id)).where(
        BrainstormRequest.status != BrainstormRequestStatus.WAITING.value
    )
    max_non_waiting = (
        await execute_db_statement(db, max_non_waiting_stmt, __name__)
    ).scalar_one_or_none()

    queue_stmt = select(func.count()).where(
        BrainstormRequest.status == BrainstormRequestStatus.WAITING.value
    )
    if max_non_waiting is not None:
        queue_stmt = queue_stmt.where(BrainstormRequest.private_id > max_non_waiting)
    queue_depth = (await execute_db_statement(db, queue_stmt, __name__)).scalar_one()

    scored_users_stmt = select(
        func.count(func.distinct(BrainstormRequest.pubkey))
    ).where(
        BrainstormRequest.status == BrainstormRequestStatus.SUCCESS.value,
        BrainstormRequest.pubkey.is_not(None),
    )
    scored_users = (
        await execute_db_statement(db, scored_users_stmt, __name__)
    ).scalar_one()

    sp_adopters_stmt = select(
        func.count(func.distinct(BrainstormRequest.pubkey))
    ).where(
        BrainstormRequest.status_ta_publication
        == BrainstormRequestStatus.SUCCESS.value,
        BrainstormRequest.pubkey.is_not(None),
    )
    sp_adopters = (
        await execute_db_statement(db, sp_adopters_stmt, __name__)
    ).scalar_one()

    return {
        "queue_depth": queue_depth,
        "scored_users": scored_users,
        "sp_adopters": sp_adopters,
    }


def build_recent_active_pubkeys_stmt(
    days: int = 30,
    search: str | None = None,
    sort: UsersSort = UsersSort.last_triggered,
    order: SortOrder = SortOrder.desc,
) -> Select:
    cutoff = datetime.now() - timedelta(days=days)

    latest_subq_q = select(
        BrainstormRequest.pubkey.label("pubkey"),
        func.count(BrainstormRequest.private_id).label("times_calculated"),
        func.max(BrainstormRequest.created_at).label("last_triggered"),
        func.max(BrainstormRequest.updated_at).label("last_updated"),
        func.max(BrainstormRequest.private_id).label("latest_id"),
    ).where(
        BrainstormRequest.created_at >= cutoff,
        BrainstormRequest.pubkey.is_not(None),
    )
    if search:
        # Accept hex (partial or full) or full npub (converted to hex).
        # TODO: also support partial display_name match (requires profile data).
        needle = search.strip()
        if needle.startswith("npub1"):
            try:
                needle = PublicKey.parse(needle).to_hex()
            except Exception:
                pass
        latest_subq_q = latest_subq_q.where(
            BrainstormRequest.pubkey.ilike(f"%{needle}%")
        )
    latest_subq = latest_subq_q.group_by(BrainstormRequest.pubkey).subquery()

    sort_col = latest_subq.c[sort.value]
    direction = asc if order == SortOrder.asc else desc

    br_latest = BrainstormRequest.__table__.alias("br_latest")
    return (
        select(
            latest_subq.c.pubkey,
            latest_subq.c.times_calculated,
            latest_subq.c.last_triggered,
            latest_subq.c.last_updated,
            br_latest.c.status.label("latest_status"),
            br_latest.c.status_ta_publication.label("latest_ta_status"),
            br_latest.c.algorithm.label("latest_algorithm"),
            BrainstormNsec.nsec.label("nsec"),
            # Effective policy: the user's explicit assignment, else NULL (the
            # router fills NULL with the default policy's name in one lookup).
            Scheduling.id.label("scheduling_id"),
            Scheduling.name.label("scheduling_name"),
        )
        .join(br_latest, br_latest.c.private_id == latest_subq.c.latest_id)
        .outerjoin(BrainstormNsec, BrainstormNsec.pubkey == latest_subq.c.pubkey)
        .outerjoin(Scheduling, Scheduling.id == BrainstormNsec.scheduling_id)
        .order_by(direction(sort_col))
    )


def build_recent_brainstorm_requests_stmt(
    pubkey: str | None = None,
    status: str | None = None,
    algorithm: str | None = None,
    days: int = 30,
) -> Select:
    cutoff = datetime.now() - timedelta(days=days)
    filters = [BrainstormRequest.created_at >= cutoff]
    if pubkey is not None:
        filters.append(BrainstormRequest.pubkey == pubkey)
    if status is not None:
        filters.append(BrainstormRequest.status == status)
    if algorithm is not None:
        filters.append(BrainstormRequest.algorithm == algorithm)

    return (
        select(BrainstormRequest)
        .where(*filters)
        .order_by(desc(BrainstormRequest.created_at))
    )


async def select_latest_non_waiting_brainstorm_request_on_db(
    db: AsyncDBSession, pubkey: str
) -> BrainstormRequest | None:
    statement = (
        select(BrainstormRequest)
        .where(BrainstormRequest.pubkey == pubkey)
        .where(BrainstormRequest.status != BrainstormRequestStatus.WAITING.value)
        .order_by(desc(BrainstormRequest.created_at))
        .limit(1)
    )

    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormRequest | None = existing_data.scalars().first()

    return result


async def update_brainstorm_request_ta_status_by_id_on_db(
    db: AsyncDBSession,
    brainstorm_request_id: int,
    status: BrainstormRequestStatus,
) -> None:
    statement = (
        update(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .values(status_ta_publication=status.value)
    )

    _ = await execute_db_statement(db, statement, __name__)
    return None


async def update_brainstorm_request_publish_duration_by_id_on_db(
    db: AsyncDBSession, brainstorm_request_id: int, seconds: float
) -> None:
    statement = (
        update(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .values(publish_duration_seconds=seconds)
    )
    await db.execute(statement)


async def update_brainstorm_request_internal_publication_status_by_id_on_db(
    db: AsyncDBSession,
    brainstorm_request_id: int,
    status: BrainstormRequestStatus,
) -> None:
    statement = (
        update(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .values(status_internal_brainstorm_publication=status.value)
    )

    _ = await execute_db_statement(db, statement, __name__)
    return None


async def update_brainstorm_request_status_by_id_on_db(
    db: AsyncDBSession,
    brainstorm_request_id: int,
    status: BrainstormRequestStatus,
) -> None:
    statement = (
        update(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .values(status=status.value)
    )

    _ = await execute_db_statement(db, statement, __name__)
    return None


async def update_brainstorm_request_result_by_id_on_db(
    db: AsyncDBSession,
    brainstorm_request_id: int,
    count_values: str,
    status: BrainstormRequestStatus,
    error: dict | None = None,
) -> None:
    statement = (
        update(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .values(
            status=status.value,
            count_values=count_values,
            error=error,
        )
    )

    _ = await execute_db_statement(db, statement, __name__)
    return None


async def fail_stale_ongoing_brainstorm_requests_on_db(
    db: AsyncDBSession, stale_threshold: timedelta
) -> int:
    cutoff = datetime.now() - stale_threshold
    statement = (
        update(BrainstormRequest)
        .where(
            BrainstormRequest.status == BrainstormRequestStatus.ONGOING.value,
            BrainstormRequest.updated_at < cutoff,
        )
        .values(status=BrainstormRequestStatus.FAILURE.value)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.rowcount


_NON_TERMINAL = [
    BrainstormRequestStatus.WAITING.value,
    BrainstormRequestStatus.ONGOING.value,
]


def in_pipeline_condition():
    """A run is still in the pipeline while its calc is pending/running, or its
    calc succeeded and publishing is pending/running. A failed calc is terminal
    even though its ta_publication may still sit at the default 'waiting'.
    Mirrors app.services.scheduler.request_in_pipeline."""
    return or_(
        BrainstormRequest.status.in_(_NON_TERMINAL),
        and_(
            BrainstormRequest.status == BrainstormRequestStatus.SUCCESS.value,
            BrainstormRequest.status_ta_publication.in_(_NON_TERMINAL),
        ),
    )


async def count_scheduled_publishing_inflight_on_db(db: AsyncDBSession) -> int:
    """Scheduled runs still in the pipeline (the backpressure signal)."""
    statement = select(func.count()).where(
        BrainstormRequest.trigger_source == TriggerSource.SCHEDULED.value,
        in_pipeline_condition(),
    )
    result = await execute_db_statement(db, statement, __name__)
    return int(result.scalar_one())


async def count_successful_manual_runs_in_window_on_db(
    db: AsyncDBSession, pubkey: str, window_start: datetime
) -> tuple[int, datetime | None]:
    """Count a user's manual runs that published successfully since window_start,
    with the oldest such run's created_at (for the quota reset time)."""
    statement = select(
        func.count(),
        func.min(BrainstormRequest.created_at),
    ).where(
        BrainstormRequest.pubkey == pubkey,
        BrainstormRequest.trigger_source == TriggerSource.MANUAL.value,
        BrainstormRequest.status_ta_publication
        == BrainstormRequestStatus.SUCCESS.value,
        BrainstormRequest.created_at >= window_start,
    )
    result = await execute_db_statement(db, statement, __name__)
    row = result.one()
    return int(row[0]), row[1]


async def any_interactive_in_pipeline_on_db(db: AsyncDBSession) -> bool:
    """True if any Manual/Admin run is still in the pipeline (calc or publish)."""
    statement = select(func.count()).where(
        BrainstormRequest.trigger_source.in_(
            [TriggerSource.MANUAL.value, TriggerSource.ADMIN.value]
        ),
        in_pipeline_condition(),
    )
    result = await execute_db_statement(db, statement, __name__)
    return int(result.scalar_one()) > 0


async def fail_stale_publishing_brainstorm_requests_on_db(
    db: AsyncDBSession, stale_threshold: timedelta
) -> int:
    """Fail hung publishes (ta_publication non-terminal past the cutoff) so a
    stuck job can't permanently block scheduler admission."""
    cutoff = datetime.now() - stale_threshold
    statement = (
        update(BrainstormRequest)
        .where(
            BrainstormRequest.status_ta_publication.in_(_NON_TERMINAL),
            BrainstormRequest.updated_at < cutoff,
        )
        .values(
            status_ta_publication=BrainstormRequestStatus.FAILURE.value,
            status=BrainstormRequestStatus.FAILURE.value,
        )
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.rowcount


async def create_brainstorm_request_on_db(
    db: AsyncDBSession,
    algorithm: str,
    parameters: str,
    pubkey: str,
    graperank_preset_used: str | None = None,
    graperank_params: dict | None = None,
    force_full_relay: bool = False,
    force_full_vespa: bool = False,
    trigger_source: str = TriggerSource.MANUAL.value,
) -> BrainstormRequest:
    new_brainstorm_request_obj = BrainstormRequest(
        algorithm=algorithm,
        parameters=parameters,
        pubkey=pubkey,
        graperank_preset_used=graperank_preset_used,
        graperank_params=graperank_params,
        force_full_relay=force_full_relay,
        force_full_vespa=force_full_vespa,
        trigger_source=trigger_source,
    )

    db.add(new_brainstorm_request_obj)

    await db.flush()

    return new_brainstorm_request_obj
