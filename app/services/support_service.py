from fastapi import HTTPException
from fastapi_pagination import Page, Params
from fastapi_pagination.api import set_page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.db_models import (
    SupportAuthor,
    SupportEventType,
    SupportMessage,
    SupportTicket,
    SupportTicketStatus,
)
from app.repos.support_repo import (
    build_admin_support_tickets_stmt,
    build_user_support_tickets_stmt,
    count_unclosed_support_tickets_on_db,
    insert_support_event_on_db,
    insert_support_message_on_db,
    insert_support_ticket_on_db,
    lock_support_filing_on_db,
    lock_support_ticket_on_db,
    select_support_events_on_db,
    select_support_messages_on_db,
    select_support_ticket_on_db,
)
from app.schemas.schemas import (
    AdminSupportEventItem,
    AdminSupportMessageItem,
    AdminSupportThread,
    AdminSupportTicketItem,
    SupportEventItem,
    SupportMessageItem,
    SupportRequester,
    SupportState,
    SupportThread,
    SupportTicketItem,
)
from app.services.support_entitlement import (
    is_entitled_to_support,
    require_entitled_to_support,
)
from app.utils.datetimes import utc_now


async def get_support_state(
    db: AsyncDBSession, pubkey: str, params: Params
) -> SupportState:
    # The list is the caller's own regardless: entitlement gates writing only.
    support_included = await is_entitled_to_support(db, pubkey)
    with set_page(Page[SupportTicketItem]):
        tickets = await paginate(
            db,
            build_user_support_tickets_stmt(pubkey),
            params=params,
            transformer=lambda rows: [
                SupportTicketItem.model_validate(r) for r in rows
            ],
        )
    return SupportState(support_included=support_included, tickets=tickets)


async def create_ticket(
    db: AsyncDBSession,
    pubkey: str,
    *,
    subject: str,
    body: str,
    category: str,
    notify_email: str | None,
    diagnostics: dict[str, str] | None,
) -> SupportTicketItem:
    await require_entitled_to_support(db, pubkey)
    await lock_support_filing_on_db(db, pubkey)
    limit = settings.support_max_open_tickets
    if await count_unclosed_support_tickets_on_db(db, pubkey) >= limit:
        # A plain string: the UI toasts `detail` verbatim.
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {limit} open tickets. "
                "Resolve one before opening another."
            ),
        )
    ticket = await insert_support_ticket_on_db(
        db, pubkey, subject, category, notify_email, diagnostics
    )
    await _append_message(db, ticket, SupportAuthor.USER.value, body, pubkey)
    await insert_support_event_on_db(
        db, ticket.id, SupportEventType.OPENED.value, SupportAuthor.USER.value, pubkey
    )
    return SupportTicketItem.model_validate(ticket)


async def get_thread(
    db: AsyncDBSession, ticket_id: int, *, pubkey: str
) -> SupportThread:
    """The caller's whole conversation.

    Not gated on entitlement: it decides whether you can start or continue a
    conversation, not whether you can read one you already had.
    """
    ticket = await select_support_ticket_on_db(db, ticket_id)
    if ticket is None or ticket.pubkey != pubkey:
        # Absent and not-yours answer alike: 403 would confirm it exists.
        raise HTTPException(status_code=404, detail="No such ticket.")
    messages = await select_support_messages_on_db(db, ticket_id)
    events = await select_support_events_on_db(db, ticket_id)
    return SupportThread(
        ticket=SupportTicketItem.model_validate(ticket),
        messages=[SupportMessageItem.model_validate(m) for m in messages],
        events=[SupportEventItem(type=e.type, at=e.at, by=e.actor) for e in events],
        diagnostics=ticket.diagnostics,
        requester=SupportRequester(
            pubkey=ticket.pubkey, notify_email=ticket.notify_email
        ),
    )


async def add_user_message(
    db: AsyncDBSession, ticket_id: int, pubkey: str, body: str
) -> SupportMessageItem:
    """A reply always puts the ticket back in support's court.

    From closed that is a reopen — there is no separate action, because a
    closed ticket is not a wall. Entitlement-gated: continuing a conversation
    is a write. Resolving is not — closing a ticket you already own stays
    open to a lapsed user.
    """
    await require_entitled_to_support(db, pubkey)
    ticket = await _locked_own_ticket(db, ticket_id, pubkey)
    message = await _append_message(db, ticket, SupportAuthor.USER.value, body, pubkey)
    await _set_status(
        db, ticket, SupportTicketStatus.OPEN.value, SupportAuthor.USER.value, pubkey
    )
    return SupportMessageItem.model_validate(message)


async def resolve_ticket(
    db: AsyncDBSession, ticket_id: int, pubkey: str
) -> SupportTicketItem:
    """The user closing their own ticket. Replying reopens it."""
    ticket = await _locked_own_ticket(db, ticket_id, pubkey)
    await _set_status(
        db, ticket, SupportTicketStatus.CLOSED.value, SupportAuthor.USER.value, pubkey
    )
    return SupportTicketItem.model_validate(ticket)


async def get_admin_support_tickets(
    db: AsyncDBSession,
    params: Params,
    *,
    status: str | None,
    category: str | None,
    pubkey: str | None,
) -> Page[AdminSupportTicketItem]:
    """The queue. Rows carry the requester, so an admin knows who they are
    talking to before typing."""
    with set_page(Page[AdminSupportTicketItem]):
        return await paginate(
            db,
            build_admin_support_tickets_stmt(status, category, pubkey),
            params=params,
            transformer=lambda rows: [
                AdminSupportTicketItem.model_validate(r) for r in rows
            ],
        )


async def get_admin_thread(db: AsyncDBSession, ticket_id: int) -> AdminSupportThread:
    """Any thread, with attribution. No ownership check — that protects users
    from each other, not support from its own queue."""
    ticket = await select_support_ticket_on_db(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="No such ticket.")
    messages = await select_support_messages_on_db(db, ticket_id)
    events = await select_support_events_on_db(db, ticket_id)
    return AdminSupportThread(
        ticket=AdminSupportTicketItem.model_validate(ticket),
        messages=[AdminSupportMessageItem.model_validate(m) for m in messages],
        events=[
            AdminSupportEventItem(
                type=e.type, at=e.at, by=e.actor, actor_pubkey=e.actor_pubkey
            )
            for e in events
        ],
        diagnostics=ticket.diagnostics,
        requester=SupportRequester(
            pubkey=ticket.pubkey, notify_email=ticket.notify_email
        ),
    )


async def add_support_message(
    db: AsyncDBSession, ticket_id: int, body: str, actor_pubkey: str
) -> AdminSupportMessageItem:
    """Support answering. From closed this reopens rather than posting into a
    closed thread — a reply nobody can see is worse than none."""
    ticket = await _locked_ticket(db, ticket_id)
    message = await _append_message(
        db, ticket, SupportAuthor.SUPPORT.value, body, actor_pubkey
    )
    await _set_status(
        db,
        ticket,
        SupportTicketStatus.ANSWERED.value,
        SupportAuthor.SUPPORT.value,
        actor_pubkey,
    )
    return AdminSupportMessageItem.model_validate(message)


async def close_ticket_as_support(
    db: AsyncDBSession, ticket_id: int, message: str | None, actor_pubkey: str
) -> AdminSupportTicketItem:
    """A closing note, if there is one, belongs to the thread — so it lands
    before the ticket closes."""
    ticket = await _locked_ticket(db, ticket_id)
    if message is not None:
        await _append_message(
            db, ticket, SupportAuthor.SUPPORT.value, message, actor_pubkey
        )
    await _set_status(
        db,
        ticket,
        SupportTicketStatus.CLOSED.value,
        SupportAuthor.SUPPORT.value,
        actor_pubkey,
    )
    return AdminSupportTicketItem.model_validate(ticket)


async def reopen_ticket_as_support(
    db: AsyncDBSession, ticket_id: int, actor_pubkey: str
) -> AdminSupportTicketItem:
    ticket = await _locked_ticket(db, ticket_id)
    await _set_status(
        db,
        ticket,
        SupportTicketStatus.OPEN.value,
        SupportAuthor.SUPPORT.value,
        actor_pubkey,
    )
    return AdminSupportTicketItem.model_validate(ticket)


async def set_ticket_category(
    db: AsyncDBSession, ticket_id: int, category: str, actor_pubkey: str
) -> AdminSupportTicketItem:
    """Users mislabel; the category drives the queue's filters."""
    ticket = await _locked_ticket(db, ticket_id)
    if ticket.category != category:
        ticket.category = category
        await insert_support_event_on_db(
            db,
            ticket.id,
            SupportEventType.RECATEGORIZED.value,
            SupportAuthor.SUPPORT.value,
            actor_pubkey,
        )
    return AdminSupportTicketItem.model_validate(ticket)


async def _locked_ticket(db: AsyncDBSession, ticket_id: int) -> SupportTicket:
    ticket = await lock_support_ticket_on_db(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="No such ticket.")
    return ticket


async def _locked_own_ticket(
    db: AsyncDBSession, ticket_id: int, pubkey: str
) -> SupportTicket:
    ticket = await _locked_ticket(db, ticket_id)
    if ticket.pubkey != pubkey:
        # Same answer as absent: a 403 would confirm the ticket exists.
        raise HTTPException(status_code=404, detail="No such ticket.")
    return ticket


async def _set_status(
    db: AsyncDBSession,
    ticket: SupportTicket,
    status: str,
    actor: str,
    actor_pubkey: str | None = None,
) -> None:
    """The only writer of `status` / `closed_at`, and the only emitter of the
    lifecycle events that go with them. A no-op move records nothing, which is
    what makes resolving twice one closure rather than two."""
    was = ticket.status
    if was == status:
        return
    closed = SupportTicketStatus.CLOSED.value
    event: str | None = None
    if status == closed:
        ticket.closed_at = utc_now()
        event = SupportEventType.CLOSED.value
    elif was == closed:
        ticket.closed_at = None
        event = SupportEventType.REOPENED.value
    # Anything else is a shuffle between live states; the message is the event.
    ticket.status = status
    if event is not None:
        await insert_support_event_on_db(db, ticket.id, event, actor, actor_pubkey)


async def _append_message(
    db: AsyncDBSession,
    ticket: SupportTicket,
    author: str,
    body: str,
    actor_pubkey: str | None = None,
) -> SupportMessage:
    """The only writer of the ticket's `last_message_*`."""
    message = await insert_support_message_on_db(
        db, ticket.id, author, body, actor_pubkey
    )
    ticket.last_message_at = message.created_at
    ticket.last_message_author = author
    return message
