from fastapi import APIRouter, Depends, Request
from fastapi_pagination import Params
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_body_schemas import CreateSupportTicketBody
from app.schemas.request_response_schemas import (
    CreateSupportTicketResponse,
    GetSupportStateResponse,
)
from app.services.support_service import create_ticket, get_support_state
from app.utils.auth.auth_models import JWTData

router = APIRouter()


@router.get(
    path="",
    summary="Whether support is included for the caller, and their own tickets",
)
async def get_support_state_endpoint(
    request: Request,
    params: Params = Depends(),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetSupportStateResponse:
    jwt_data: JWTData = request.state.jwt_data
    result = await get_support_state(db, jwt_data.nostr_pubkey, params)
    return GetSupportStateResponse(data=result)


@router.post(
    path="/tickets",
    summary="File a support ticket (403 if support isn't included, 409 past the cap)",
)
async def create_support_ticket_endpoint(
    request: Request,
    body: CreateSupportTicketBody,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> CreateSupportTicketResponse:
    jwt_data: JWTData = request.state.jwt_data
    ticket = await create_ticket(
        db,
        jwt_data.nostr_pubkey,
        subject=body.subject,
        body=body.body,
        category=body.category,
        notify_email=body.notify_email,
        diagnostics=body.diagnostics,
    )
    return CreateSupportTicketResponse(data=ticket)
