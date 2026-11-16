"""Accounts whose key is compromised (`compromised_pubkey`). The set is tiny and
read once per pipeline stage per run."""

from datetime import timedelta
from typing import cast

from sqlalchemy import and_, delete, func, or_, select, update
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


async def select_compromised_by_successor_on_db(
    db: AsyncDBSession, pubkeys: list[str]
) -> list[CompromisedPubkey]:
    """The marked rows whose `successor_pubkey` is among `pubkeys`."""
    result = await db.execute(
        select(CompromisedPubkey).where(CompromisedPubkey.successor_pubkey.in_(pubkeys))
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
    successor_event_id: str | None = None,
) -> CompromisedPubkey:
    """Insert or replace every field but `created_at`."""
    stmt = insert(CompromisedPubkey).values(
        pubkey=pubkey,
        proof_event=proof_event,
        proof_event_id=proof_event["id"],
        successor_pubkey=successor_pubkey,
        successor_event_id=successor_event_id,
        reason=reason,
        added_by=added_by,
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=[CompromisedPubkey.pubkey],
        set_={
            "proof_event": stmt.excluded.proof_event,
            "proof_event_id": stmt.excluded.proof_event_id,
            "successor_pubkey": stmt.excluded.successor_pubkey,
            "successor_event_id": stmt.excluded.successor_event_id,
            "reason": stmt.excluded.reason,
            "added_by": stmt.excluded.added_by,
            "updated_at": func.now(),
            "zero_requested_at": func.now(),
        },
    ).returning(CompromisedPubkey)
    result = await db.execute(upsert)
    return result.scalar_one()


async def update_compromised_metadata_on_db(
    db: AsyncDBSession, pubkey: str, values: dict
) -> CompromisedPubkey | None:
    """Set the successor and/or note; `zero_requested_at` stays, so nothing is
    zeroed again. None when the account isn't marked."""
    result = await db.execute(
        update(CompromisedPubkey)
        .where(CompromisedPubkey.pubkey == pubkey)
        .values(**values)
        .returning(CompromisedPubkey)
    )
    return result.scalar_one_or_none()


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


async def select_compromised_due_on_db(
    db: AsyncDBSession, pending_grace: timedelta, retry_after: timedelta
) -> list[str]:
    """Accounts whose zeroing should run again: one that never reported since it
    was last asked for, `pending_grace` after that (the run died with its pod),
    and one whose last zeroing failed or was incomplete, `retry_after` after it.
    Compared in the database, on its own clock, like the writes."""
    c = CompromisedPubkey
    now = func.now()
    never_reported = and_(
        or_(c.last_zeroed_at.is_(None), c.last_zeroed_at < c.zero_requested_at),
        c.zero_requested_at < now - pending_grace,
    )
    report = c.last_zero_report
    unsuccessful = and_(
        c.last_zeroed_at >= c.zero_requested_at,
        c.last_zeroed_at < now - retry_after,
        or_(
            report.has_key("error"),
            report["complete"].as_boolean().is_not(True),
            report["relay_read"].as_boolean().is_not(True),
        ),
    )
    result = await db.execute(
        select(c.pubkey)
        .where(or_(never_reported, unsuccessful))
        .order_by(c.zero_requested_at)
    )
    return list(result.scalars().all())
