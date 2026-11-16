"""Accounts whose key is compromised (`compromised_pubkey`). The set is tiny and
read once per pipeline stage per run."""

from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import CompromisedPubkey


async def select_compromised_pubkeys_on_db(db: AsyncDBSession) -> set[str]:
    result = await db.execute(select(CompromisedPubkey.pubkey))
    return set(result.scalars().all())


async def list_compromised_on_db(db: AsyncDBSession) -> list[CompromisedPubkey]:
    result = await db.execute(
        select(CompromisedPubkey).order_by(CompromisedPubkey.created_at.desc())
    )
    return list(result.scalars().all())


async def upsert_compromised_on_db(
    db: AsyncDBSession,
    pubkey: str,
    proof_event_id: str,
    reason: str | None,
    added_by: str | None,
) -> CompromisedPubkey:
    stmt = insert(CompromisedPubkey).values(
        pubkey=pubkey,
        proof_event_id=proof_event_id,
        reason=reason,
        added_by=added_by,
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=[CompromisedPubkey.pubkey],
        set_={
            "proof_event_id": stmt.excluded.proof_event_id,
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
