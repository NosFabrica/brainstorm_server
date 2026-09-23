"""The responder side of support. Admin-gated at inclusion, like the siblings.

Bare `response_model`s, no success envelope — the admin convention.
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi_pagination import Page, Params
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.support_repo import select_admin_support_digest_on_db
from app.schemas.request_body_schemas import (
    CloseSupportTicketBody,
    CreateSupportMessageBody,
    UpdateSupportTicketBody,
)
from app.schemas.schemas import (
    AdminSupportMessageItem,
    AdminSupportThread,
    AdminSupportTicketItem,
)
from app.services.support_service import (
    add_support_message,
    close_ticket_as_support,
    get_admin_support_tickets,
    get_admin_thread,
    reopen_ticket_as_support,
    set_ticket_category,
)
from app.utils.auth.auth_models import JWTData
from app.utils.etags import etag_digest, not_modified, tag_response

router = APIRouter()


@router.get(
    path="/tickets",
    response_model=Page[AdminSupportTicketItem],
    summary="Support: the queue, newest activity first",
)
async def list_support_tickets_endpoint(
    request: Request,
    response: Response,
    params: Params = Depends(),
    status: str | None = None,
    category: str | None = None,
    pubkey: str | None = None,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    # Every filter is in the tag, or page two comes back unchanged against
    # page one's and the queue renders stale rows.
    latest, count = await select_admin_support_digest_on_db(
        db, status, category, pubkey
    )
    etag = etag_digest(
        latest, count, status, category, pubkey, params.page, params.size
    )
    if request.headers.get("if-none-match") == etag:
        return not_modified(etag)

    page = await get_admin_support_tickets(
        db, params, status=status, category=category, pubkey=pubkey
    )
    tag_response(response, etag)
    return page


@router.get(
    path="/tickets/{ticket_id}",
    response_model=AdminSupportThread,
    summary="Support: any thread, with attribution",
)
async def get_support_thread_endpoint(
    ticket_id: int,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    return await get_admin_thread(db, ticket_id)


@router.post(
    path="/tickets/{ticket_id}/messages",
    response_model=AdminSupportMessageItem,
    summary="Support: answer a ticket (reopens it if it was closed)",
)
async def create_support_message_endpoint(
    request: Request,
    ticket_id: int,
    body: CreateSupportMessageBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    return await add_support_message(db, ticket_id, body.body, jwt_data.nostr_pubkey)


@router.post(
    path="/tickets/{ticket_id}/close",
    response_model=AdminSupportTicketItem,
    summary="Support: close a ticket, optionally with a closing note",
)
async def close_support_ticket_endpoint(
    request: Request,
    ticket_id: int,
    body: CloseSupportTicketBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    return await close_ticket_as_support(
        db, ticket_id, body.message, jwt_data.nostr_pubkey
    )


@router.post(
    path="/tickets/{ticket_id}/reopen",
    response_model=AdminSupportTicketItem,
    summary="Support: reopen a ticket without saying anything",
)
async def reopen_support_ticket_endpoint(
    request: Request,
    ticket_id: int,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    return await reopen_ticket_as_support(db, ticket_id, jwt_data.nostr_pubkey)


@router.patch(
    path="/tickets/{ticket_id}",
    response_model=AdminSupportTicketItem,
    summary="Support: recategorize a ticket",
)
async def update_support_ticket_endpoint(
    request: Request,
    ticket_id: int,
    body: UpdateSupportTicketBody,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    jwt_data: JWTData = request.state.jwt_data
    return await set_ticket_category(
        db, ticket_id, body.category, jwt_data.nostr_pubkey
    )
