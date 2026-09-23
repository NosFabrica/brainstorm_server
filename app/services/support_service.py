from fastapi import HTTPException
from fastapi_pagination import Page, Params
from fastapi_pagination.api import set_page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.db_models import SupportAuthor, SupportEventType, SupportMessage, SupportTicket
from app.repos.support_repo import (
    build_user_support_tickets_stmt,
    count_unclosed_support_tickets_on_db,
    insert_support_event_on_db,
    insert_support_message_on_db,
    insert_support_ticket_on_db,
    lock_support_filing_on_db,
)
from app.schemas.schemas import SupportState, SupportTicketItem
from app.services.support_entitlement import (
    is_entitled_to_support,
    require_entitled_to_support,
)


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
