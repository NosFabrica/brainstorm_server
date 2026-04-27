from datetime import datetime
from sqlalchemy import select, update
from app.core.database import execute_db_statement, handle_no_data
from app.db_models import BrainstormNsec
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.utils.encryption import decrypt_nsec, encrypt_nsec
from app.utils.nostr import generate_random_nsec


def _resolve_plaintext_nsec(row: BrainstormNsec) -> str:
    """Prefer encrypted_nsec if present, otherwise fall back to plaintext nsec."""
    if row.encrypted_nsec:
        return decrypt_nsec(row.encrypted_nsec)
    return row.nsec


async def get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> tuple[BrainstormNsec, bool]:
    stmt = select(BrainstormNsec).where(BrainstormNsec.pubkey == pubkey)
    existing_data = await execute_db_statement(db, stmt, __name__)
    result: BrainstormNsec | None = existing_data.scalar_one_or_none()
    if result:
        result.nsec = _resolve_plaintext_nsec(result)
        return result, False

    # Create new one - dual-write: plaintext in nsec, encrypted in encrypted_nsec
    nsec = generate_random_nsec()
    instance = BrainstormNsec(
        pubkey=pubkey,
        nsec=nsec,
        encrypted_nsec=encrypt_nsec(nsec),
    )

    db.add(instance)

    await db.flush()
    await db.refresh(instance)

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


async def get_graperank_preset_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> str | None:
    statement = select(BrainstormNsec.graperank_preset).where(
        BrainstormNsec.pubkey == pubkey
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def set_graperank_preset_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str, preset: str
) -> None:
    await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey)
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(graperank_preset=preset)
    )
    await db.execute(statement)


async def get_graperank_custom_params_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> dict | None:
    statement = select(BrainstormNsec.graperank_custom_params).where(
        BrainstormNsec.pubkey == pubkey
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def set_graperank_custom_params_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str, params: dict
) -> None:
    await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey)
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(graperank_custom_params=params)
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

    result.nsec = _resolve_plaintext_nsec(result)
    return result
