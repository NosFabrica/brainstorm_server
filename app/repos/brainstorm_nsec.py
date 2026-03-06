from datetime import datetime
from sqlalchemy import select, update
from app.core.database import execute_db_statement, handle_no_data
from app.db_models import BrainstormNsec
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.utils.nostr import generate_random_nsec


async def get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> tuple[BrainstormNsec, bool]:
    stmt = select(BrainstormNsec).where(BrainstormNsec.pubkey == pubkey)
    existing_data = await execute_db_statement(db, stmt, __name__)
    result: BrainstormNsec | None = existing_data.scalar_one_or_none()
    if result:
        return result, False

    # Create new one
    nsec = generate_random_nsec()
    instance = BrainstormNsec(
        pubkey=pubkey,
        nsec=nsec,
    )

    db.add(instance)

    await db.flush()

    return instance, True


async def update_last_time_triggered_graperank_on_db(
    db: AsyncDBSession,
    pubkey: str,
    when: datetime | None = None,
) -> None:
    when = when or datetime.now()

    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(last_time_triggered_graperank=when)
    )

    await db.execute(statement)


async def update_last_time_calculated_graperank_on_db(
    db: AsyncDBSession,
    pubkey: str,
    when: datetime | None = None,
) -> None:
    when = when or datetime.now()

    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(last_time_calculated_graperank=when)
    )

    await db.execute(statement)


async def select_brainstorm_nsec_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> BrainstormNsec:
    statement = select(BrainstormNsec).where(BrainstormNsec.pubkey == pubkey)

    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormNsec | None = existing_data.scalars().first()

    handle_no_data(result)
    assert result

    return result
