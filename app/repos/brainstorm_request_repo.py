from datetime import datetime, timedelta

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.orm import defer
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement, handle_no_data
from app.core.loggr import loggr
from app.db_models import BrainstormRequest, BrainstormRequestStatus

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
