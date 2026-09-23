from fastapi import APIRouter, Depends, Request
from fastapi_pagination import Params
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_response_schemas import GetSupportStateResponse
from app.services.support_service import get_support_state
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
