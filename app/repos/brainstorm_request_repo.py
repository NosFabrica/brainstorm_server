from datetime import datetime, timedelta

from sqlalchemy import asc, delete, desc, func, select, update
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement, handle_no_data
from app.core.loggr import loggr
from app.db_models import BrainstormNsec, BrainstormRequest, BrainstormRequestStatus

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


async def select_brainstorm_request_by_id_and_password_on_db(
    db: AsyncDBSession,
    brainstorm_request_id: int,
    brainstorm_request_password: str,
    include_result: bool = False,
) -> BrainstormRequest:
    statement = select(BrainstormRequest).where(
        BrainstormRequest.private_id == brainstorm_request_id,
        BrainstormRequest.password == brainstorm_request_password,
    )
    if not include_result:
        statement = statement.options(defer(BrainstormRequest.result))
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
        .options(defer(BrainstormRequest.result))
        .limit(1)
    )

    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormRequest | None = existing_data.scalars().first()

    return result


async def compute_admin_stats_on_db(db: AsyncDBSession) -> dict:
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
    queue_depth = (
        await execute_db_statement(db, queue_stmt, __name__)
    ).scalar_one()

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


_USERS_SORT_COLUMNS = {
    "pubkey": "pubkey",
    "times_calculated": "times_calculated",
    "last_triggered": "last_triggered",
    "last_updated": "last_updated",
}


async def select_recent_active_pubkeys_on_db(
    db: AsyncDBSession,
    days: int = 30,
    page: int = 0,
    limit: int = 25,
    search: str | None = None,
    sort: str = "last_triggered",
    order: str = "desc",
) -> tuple[list[dict], int]:
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
        latest_subq_q = latest_subq_q.where(
            BrainstormRequest.pubkey.ilike(f"%{search}%")
        )
    latest_subq = latest_subq_q.group_by(BrainstormRequest.pubkey).subquery()

    count_stmt = select(func.count()).select_from(latest_subq)
    total_result = await execute_db_statement(db, count_stmt, __name__)
    total: int = total_result.scalar_one()

    sort_col_name = _USERS_SORT_COLUMNS.get(sort, "last_triggered")
    sort_col = latest_subq.c[sort_col_name]
    direction = asc if order.lower() == "asc" else desc

    br_latest = BrainstormRequest.__table__.alias("br_latest")
    statement = (
        select(
            latest_subq.c.pubkey,
            latest_subq.c.times_calculated,
            latest_subq.c.last_triggered,
            latest_subq.c.last_updated,
            br_latest.c.status.label("latest_status"),
            br_latest.c.status_ta_publication.label("latest_ta_status"),
            br_latest.c.algorithm.label("latest_algorithm"),
            BrainstormNsec.nsec.label("nsec"),
        )
        .join(br_latest, br_latest.c.private_id == latest_subq.c.latest_id)
        .outerjoin(BrainstormNsec, BrainstormNsec.pubkey == latest_subq.c.pubkey)
        .order_by(direction(sort_col))
        .offset(page * limit)
        .limit(limit)
    )

    rows_result = await execute_db_statement(db, statement, __name__)
    rows = [dict(r._mapping) for r in rows_result.all()]
    return rows, total


async def select_recent_brainstorm_requests_on_db(
    db: AsyncDBSession,
    pubkey: str | None = None,
    status: str | None = None,
    algorithm: str | None = None,
    days: int = 30,
) -> list[BrainstormRequest]:
    cutoff = datetime.now() - timedelta(days=days)
    statement = (
        select(BrainstormRequest)
        .where(BrainstormRequest.created_at >= cutoff)
        .order_by(desc(BrainstormRequest.created_at))
        .options(defer(BrainstormRequest.result))
    )
    if pubkey is not None:
        statement = statement.where(BrainstormRequest.pubkey == pubkey)
    if status is not None:
        statement = statement.where(BrainstormRequest.status == status)
    if algorithm is not None:
        statement = statement.where(BrainstormRequest.algorithm == algorithm)

    existing_data = await execute_db_statement(db, statement, __name__)
    return list(existing_data.scalars().all())


async def select_latest_successful_brainstorm_request_on_db(
    db: AsyncDBSession, pubkey: str
) -> BrainstormRequest | None:
    statement = (
        select(BrainstormRequest)
        .where(BrainstormRequest.pubkey == pubkey)
        .where(BrainstormRequest.status == BrainstormRequestStatus.SUCCESS.value)
        .order_by(desc(BrainstormRequest.created_at))
        .limit(1)
    )

    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormRequest | None = existing_data.scalars().first()

    return result


async def select_brainstorm_request_by_id_on_db(
    db: AsyncDBSession, brainstorm_request_id: int
) -> BrainstormRequest:
    statement = (
        select(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .options(defer(BrainstormRequest.result))
    )
    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormRequest | None = existing_data.scalars().first()

    handle_no_data(result)
    assert result

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
    result: str,
    count_values: str,
    status: BrainstormRequestStatus,
) -> None:
    statement = (
        update(BrainstormRequest)
        .where(BrainstormRequest.private_id == brainstorm_request_id)
        .values(result=result, status=status.value, count_values=count_values)
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


async def create_brainstorm_request_on_db(
    db: AsyncDBSession, algorithm: str, parameters: str, pubkey: str
) -> BrainstormRequest:
    new_brainstorm_request_obj = BrainstormRequest(
        algorithm=algorithm, parameters=parameters, pubkey=pubkey
    )

    db.add(new_brainstorm_request_obj)

    await db.flush()

    return new_brainstorm_request_obj
