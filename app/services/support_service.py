from fastapi_pagination import Page, Params
from fastapi_pagination.api import set_page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.support_repo import build_user_support_tickets_stmt
from app.schemas.schemas import SupportState, SupportTicketItem
from app.services.support_entitlement import is_entitled_to_support


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
