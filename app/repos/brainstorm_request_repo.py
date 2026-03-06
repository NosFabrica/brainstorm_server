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
    db: AsyncDBSession, brainstorm_request_id: int, brainstorm_request_password: str
) -> BrainstormRequest:
    statement = (
        select(BrainstormRequest)
        .where(
            BrainstormRequest.private_id == brainstorm_request_id,
            BrainstormRequest.password == brainstorm_request_password,
        )
        .options(defer(BrainstormRequest.result))
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
    statement = select(func.count()).where(
        BrainstormRequest.private_id < reference_id,
        BrainstormRequest.status.in_(
            [
                BrainstormRequestStatus.ONGOING.value,
                BrainstormRequestStatus.WAITING.value,
            ]
        ),
    )

    result = await db.execute(statement)
    return result.scalar_one()


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


async def create_brainstorm_request_on_db(
    db: AsyncDBSession, algorithm: str, parameters: str, pubkey: str
) -> BrainstormRequest:
    new_brainstorm_request_obj = BrainstormRequest(
        algorithm=algorithm, parameters=parameters, pubkey=pubkey
    )

    db.add(new_brainstorm_request_obj)

    await db.flush()

    return new_brainstorm_request_obj
