from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm import defer

from app.core.database import execute_db_statement, handle_no_data
from app.db_models import BrainstormNsec, Scheduling
from app.repos.scheduling_repo import (
    get_default_scheduling_on_db,
    get_scheduling_on_db,
)
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
    # Defer the heavy columns nobody on this path needs — most notably
    # last_published_pubkeys (LargeBinary, ~3MB for a big observer). Callers only
    # read nsec/pubkey/timestamps; deferring keeps the per-call SELECT off the
    # multi-MB blob. (Mirrors select_brainstorm_nsec_history_fields_on_db.)
    stmt = (
        select(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .options(
            defer(BrainstormNsec.last_published_pubkeys),
            defer(BrainstormNsec.last_published_graperank_request_id),
            defer(BrainstormNsec.graperank_custom_params),
        )
    )
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


async def update_last_time_published_graperank_on_db(
    db: AsyncDBSession,
    pubkey: str,
    when: datetime | None = None,
) -> None:
    when = when or datetime.now()
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(last_time_published_graperank=when)
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


# 32-byte raw nostr pubkey, packed back-to-back (no separator) for compact storage.
_PUBKEY_BYTES = 32


def _pack_pubkeys(pubkeys: list[str]) -> bytes:
    return b"".join(bytes.fromhex(pk) for pk in pubkeys)


def _unpack_pubkeys(blob: bytes | None) -> list[str]:
    if not blob:
        return []
    return [
        blob[i : i + _PUBKEY_BYTES].hex() for i in range(0, len(blob), _PUBKEY_BYTES)
    ]


async def get_last_published_pubkeys_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> list[str]:
    statement = select(BrainstormNsec.last_published_pubkeys).where(
        BrainstormNsec.pubkey == pubkey
    )
    result = await execute_db_statement(db, statement, __name__)
    return _unpack_pubkeys(result.scalar_one_or_none())


async def update_last_published_pubkeys_by_pubkey_on_db(
    db: AsyncDBSession,
    pubkey: str,
    published_pubkeys: list[str],
    graperank_request_id: int,
) -> None:
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(
            last_published_pubkeys=_pack_pubkeys(published_pubkeys),
            last_published_graperank_request_id=graperank_request_id,
        )
    )
    await db.execute(statement)


async def get_scheduling_for_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> Scheduling | None:
    """The scheduling policy in effect for a user: their explicit assignment,
    else the default policy. Unset / pre-existing users resolve to the default.
    """
    statement = select(BrainstormNsec.scheduling_id).where(
        BrainstormNsec.pubkey == pubkey
    )
    result = await execute_db_statement(db, statement, __name__)
    scheduling_id = result.scalar_one_or_none()
    if scheduling_id is not None:
        row = await get_scheduling_on_db(db, scheduling_id)
        if row is not None:
            return row
    return await get_default_scheduling_on_db(db)


async def set_scheduling_for_pubkey_on_db(
    db: AsyncDBSession, pubkey: str, scheduling_id: int
) -> None:
    """Assign a user to a scheduling policy (auto-creating the row if absent).

    This is the single seam a future admin-CRUD or external service would reuse
    to move a user between policies.
    """
    await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey)
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(scheduling_id=scheduling_id)
    )
    await db.execute(statement)


async def set_is_observer_search_available_by_pubkey_on_db(
    db: AsyncDBSession,
    pubkey: str,
    is_available: bool = True,
) -> None:
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(is_observer_search_available=is_available)
    )
    await db.execute(statement)


async def increment_runs_since_full_on_db(db: AsyncDBSession, pubkey: str) -> None:
    """Count one more scheduled delta toward the every-Nth full backstop."""
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(runs_since_full=BrainstormNsec.runs_since_full + 1)
    )
    await db.execute(statement)


async def reset_runs_since_full_on_db(db: AsyncDBSession, pubkey: str) -> None:
    """Clear the backstop counter after a successful full run (sink in sync)."""
    statement = (
        update(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .values(runs_since_full=0)
    )
    await db.execute(statement)


async def get_is_observer_search_available_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> bool:
    statement = select(BrainstormNsec.is_observer_search_available).where(
        BrainstormNsec.pubkey == pubkey
    )
    result = await execute_db_statement(db, statement, __name__)
    return bool(result.scalar_one_or_none())


async def brainstorm_nsec_exists_by_pubkey_on_db(
    db: AsyncDBSession, pubkey: str
) -> bool:
    statement = select(BrainstormNsec.pubkey).where(BrainstormNsec.pubkey == pubkey)
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None


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


async def select_brainstorm_nsec_history_fields_on_db(
    db: AsyncDBSession, pubkey: str
) -> BrainstormNsec:
    # Skip large columns not needed by the user-history converter — most notably
    # last_published_pubkeys (LargeBinary, can be 100s of KB) and graperank_custom_params (JSONB).
    statement = (
        select(BrainstormNsec)
        .where(BrainstormNsec.pubkey == pubkey)
        .options(
            defer(BrainstormNsec.last_published_pubkeys),
            defer(BrainstormNsec.last_published_graperank_request_id),
            defer(BrainstormNsec.graperank_preset),
            defer(BrainstormNsec.graperank_custom_params),
        )
    )

    existing_data = await execute_db_statement(db, statement, __name__)
    result: BrainstormNsec | None = existing_data.scalars().first()

    handle_no_data(result)
    assert result

    result.nsec = _resolve_plaintext_nsec(result)
    return result
