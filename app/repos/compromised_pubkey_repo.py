"""Accounts whose key is compromised (`compromised_pubkey`). The set is tiny and
read once per pipeline stage per run."""

from typing import cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import CompromisedPubkey


async def select_compromised_pubkeys_on_db(db: AsyncDBSession) -> set[str]:
    result = await db.execute(select(CompromisedPubkey.pubkey))
    return set(result.scalars().all())


async def select_compromised_in_on_db(
    db: AsyncDBSession, pubkeys: list[str]
) -> list[CompromisedPubkey]:
    """The marked rows among `pubkeys` (a primary-key lookup)."""
    result = await db.execute(
        select(CompromisedPubkey).where(CompromisedPubkey.pubkey.in_(pubkeys))
    )
    return list(result.scalars().all())


async def list_compromised_on_db(db: AsyncDBSession) -> list[CompromisedPubkey]:
    result = await db.execute(
        select(CompromisedPubkey).order_by(CompromisedPubkey.created_at.desc())
    )
    return list(result.scalars().all())


async def select_compromised_on_db(
    db: AsyncDBSession, pubkey: str
) -> CompromisedPubkey | None:
    result = await db.execute(
        select(CompromisedPubkey).where(CompromisedPubkey.pubkey == pubkey)
    )
    return result.scalar_one_or_none()


async def upsert_compromised_on_db(
    db: AsyncDBSession,
    pubkey: str,
    proof_event: dict,
    successor_pubkey: str | None,
    reason: str | None,
    added_by: str | None,
) -> CompromisedPubkey:
    """Insert or replace every field but `created_at`."""
    stmt = insert(CompromisedPubkey).values(
        pubkey=pubkey,
        proof_event=proof_event,
        proof_event_id=proof_event["id"],
        successor_pubkey=successor_pubkey,
        reason=reason,
        added_by=added_by,
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=[CompromisedPubkey.pubkey],
        set_={
            "proof_event": stmt.excluded.proof_event,
            "proof_event_id": stmt.excluded.proof_event_id,
            "successor_pubkey": stmt.excluded.successor_pubkey,
            "reason": stmt.excluded.reason,
            "added_by": stmt.excluded.added_by,
            "updated_at": func.now(),
        },
    ).returning(CompromisedPubkey)
    result = await db.execute(upsert)
    return result.scalar_one()


async def delete_compromised_on_db(db: AsyncDBSession, pubkey: str) -> bool:
    result = cast(
        CursorResult,
        await db.execute(
            delete(CompromisedPubkey).where(CompromisedPubkey.pubkey == pubkey)
        ),
    )
    return bool(result.rowcount)


async def record_zero_report_on_db(
    db: AsyncDBSession, pubkey: str, report: dict
) -> None:
    await db.execute(
        update(CompromisedPubkey)
        .where(CompromisedPubkey.pubkey == pubkey)
        .values(last_zeroed_at=func.now(), last_zero_report=report)
    )
