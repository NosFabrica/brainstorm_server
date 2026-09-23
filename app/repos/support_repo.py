from datetime import timedelta
from typing import TypeVar, cast

from sqlalchemy import CursorResult, Select, Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import (
    SupportEvent,
    SupportMessage,
    SupportTicket,
    SupportTicketStatus,
)
from app.utils.datetimes import utc_now

_Row = TypeVar("_Row", SupportTicket, SupportMessage, SupportEvent)


async def _insert(db: AsyncDBSession, row: _Row) -> _Row:
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


def build_user_support_tickets_stmt(pubkey: str) -> Select:
    return (
        select(SupportTicket)
        .where(SupportTicket.pubkey == pubkey)
        .order_by(SupportTicket.last_message_at.desc(), SupportTicket.id.desc())
    )


async def select_support_ticket_on_db(
    db: AsyncDBSession, ticket_id: int
) -> SupportTicket | None:
    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
    )
    return result.scalar_one_or_none()


async def lock_support_ticket_on_db(
    db: AsyncDBSession, ticket_id: int
) -> SupportTicket | None:
    """The ticket, held until commit. Every mutation read-modify-writes it."""
    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def select_support_messages_on_db(
    db: AsyncDBSession, ticket_id: int
) -> list[SupportMessage]:
    """Ordered by id: `created_at` ties when two land in the same millisecond."""
    result = await db.execute(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket_id)
        .order_by(SupportMessage.id)
    )
    return list(result.scalars().all())


async def select_support_events_on_db(
    db: AsyncDBSession, ticket_id: int
) -> list[SupportEvent]:
    result = await db.execute(
        select(SupportEvent)
        .where(SupportEvent.ticket_id == ticket_id)
        .order_by(SupportEvent.id)
    )
    return list(result.scalars().all())


def build_admin_support_tickets_stmt(
    status: str | None, category: str | None, pubkey: str | None
) -> Select:
    stmt = select(SupportTicket)
    if status is not None:
        stmt = stmt.where(SupportTicket.status == status)
    if category is not None:
        stmt = stmt.where(SupportTicket.category == category)
    if pubkey is not None:
        stmt = stmt.where(SupportTicket.pubkey == pubkey)
    return stmt.order_by(SupportTicket.last_message_at.desc(), SupportTicket.id.desc())


async def lock_support_filing_on_db(db: AsyncDBSession, pubkey: str) -> None:
    """Serialize one caller's filings until commit, so the cap count holds."""
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(pubkey))))


async def count_unclosed_support_tickets_on_db(db: AsyncDBSession, pubkey: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(SupportTicket)
        .where(
            SupportTicket.pubkey == pubkey,
            SupportTicket.status != SupportTicketStatus.CLOSED.value,
        )
    )
    return result.scalar_one()


async def insert_support_ticket_on_db(
    db: AsyncDBSession,
    pubkey: str,
    subject: str,
    category: str,
    notify_email: str | None,
    diagnostics: dict[str, str] | None,
) -> SupportTicket:
    row = SupportTicket(
        pubkey=pubkey,
        subject=subject,
        category=category,
        status=SupportTicketStatus.OPEN.value,
        notify_email=notify_email,
        diagnostics=diagnostics,
    )
    return await _insert(db, row)


async def insert_support_message_on_db(
    db: AsyncDBSession,
    ticket_id: int,
    author: str,
    body: str,
    actor_pubkey: str | None,
) -> SupportMessage:
    row = SupportMessage(
        ticket_id=ticket_id, author=author, body=body, actor_pubkey=actor_pubkey
    )
    return await _insert(db, row)


async def insert_support_event_on_db(
    db: AsyncDBSession,
    ticket_id: int,
    event_type: str,
    actor: str,
    actor_pubkey: str | None,
) -> SupportEvent:
    row = SupportEvent(
        ticket_id=ticket_id, type=event_type, actor=actor, actor_pubkey=actor_pubkey
    )
    return await _insert(db, row)


def build_expired_diagnostics_stmt(retention: timedelta) -> Update:
    """Clear the snapshot on tickets filed longer ago than `retention`.

    Dated from filing, not closing: the snapshot describes the moment it was
    taken and is stale well before the window is out, and dating from closure
    would let a ticket nobody ever closes keep its snapshot forever. Only the
    snapshot goes — the ticket, its messages and its events stay.
    """
    return (
        update(SupportTicket)
        .where(
            SupportTicket.created_at < utc_now() - retention,
            # Without this, every sweep rewrites every old row forever.
            SupportTicket.diagnostics.is_not(None),
        )
        .values(diagnostics=None)
    )


async def clear_expired_support_diagnostics_on_db(
    db: AsyncDBSession, retention: timedelta
) -> int:
    result = await db.execute(build_expired_diagnostics_stmt(retention))
    # DML results are CursorResult at runtime; the base Result stub lacks rowcount.
    return cast(CursorResult, result).rowcount
