from datetime import datetime, timedelta
from fastapi import HTTPException
from fastapi import APIRouter, Depends, Request, status
from app.core.database import get_db
from app.schemas.request_body_schemas import SubmitNostrAuthChallengeBody
from app.schemas.request_response_schemas import (
    GetOwnLatestGraperankResponse,
    GetOwnUserDataResponse,
    GetUserDataResponse,
)


from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from app.schemas.schemas import OwnUserData
from app.services.brainstorm_request_service import create_brainstorm_request
from app.services.user_service import (
    get_own_latest_graperank,
    get_user_graph_data,
    get_user_history_data,
)
from app.utils.auth.auth_models import JWTData

CHALLENGE_TTL = 120  # seconds (2 minutes)

router = APIRouter()


@router.get(
    path="/graperankResult",
    tags=[],
    dependencies=[],
    summary="Get own graperank result endpoint (latest result)",
)
async def get_own_latest_graperank_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnLatestGraperankResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    result = await get_own_latest_graperank(db, user_pubkey)

    return GetOwnLatestGraperankResponse(data=result)


@router.post(
    path="/graperank",
    tags=[],
    dependencies=[],
    summary="Start a graperank calculation",
)
async def create_graperank_calc_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnLatestGraperankResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    # latest = await get_own_latest_graperank(db, user_pubkey)

    # if latest and latest.created_at > datetime.now() - timedelta(hours=1):
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="The last triggered Graperank was too recent",
    #     )

    result = await create_brainstorm_request(
        db=db,
        algorithm="graperank",
        parameters=user_pubkey,
        pubkey=user_pubkey,
        nsec_exists=True,
    )

    return GetOwnLatestGraperankResponse(data=result)


@router.get(
    path="/self",
    tags=[],
    dependencies=[],
    summary="Get own user data endpoint",
)
async def get_own_user_data_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnUserDataResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    result = await get_user_graph_data(user_pubkey)

    history = await get_user_history_data(db, user_pubkey)

    return GetOwnUserDataResponse(data=OwnUserData(graph=result, history=history))


@router.get(
    path="/{pubkey}",
    tags=[],
    dependencies=[],
    summary="Get user by pubkey data endpoint",
)
async def get_user_by_pubkey_data_endpoint(
    request: Request, pubkey: str
) -> GetUserDataResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    result = await get_user_graph_data(pubkey, user_pubkey)

    return GetUserDataResponse(data=result)
