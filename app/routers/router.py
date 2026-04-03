from fastapi import Query
from fastapi import APIRouter, Depends, Request

from app.core.database import get_db

# from app.routers.brainstorm_request.router import router as brainstorm_request_router
from app.routers.admin.router import router as admin_router
from app.routers.auth_challenge.router import router as auth_challenge_router
from app.routers.setup.router import router as setup_router
from app.routers.user.router import router as user_router
from app.schemas.request_response_schemas import (
    GetWhitelistedPubkeysOfObserverResponse,
    WhitelistedPubkeys,
)
from app.services.user_service import (
    get_whitelisted_pubkeys_of_observer,
)
from app.utils.api_validators import verify_token
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

router = APIRouter()

ADMIN_ROUTER_PREFIX = "/admin"

router.include_router(
    router=admin_router,
    prefix=ADMIN_ROUTER_PREFIX,
    tags=["admin"],
)

# BRAINSTORM_REQUEST_ROUTER_PREFIX = "/brainstormRequest"

# router.include_router(
#     router=brainstorm_request_router,
#     prefix=BRAINSTORM_REQUEST_ROUTER_PREFIX,
#     tags=["brainstorm_request"],
# )

AUTH_CHALLENGE_ROUTER_PREFIX = "/authChallenge"

router.include_router(
    router=auth_challenge_router,
    prefix=AUTH_CHALLENGE_ROUTER_PREFIX,
    tags=["auth_challenge"],
)

SETUP_ROUTER_PREFIX = "/setup"

router.include_router(
    router=setup_router,
    prefix=SETUP_ROUTER_PREFIX,
    tags=["setup"],
)

USER_ROUTER_PREFIX = "/user"

router.include_router(
    dependencies=[Depends(verify_token)],
    router=user_router,
    prefix=USER_ROUTER_PREFIX,
    tags=["user"],
)


@router.get(
    path="/whitelisted/{observer_pubkey}",
    tags=[],
    dependencies=[],
    summary="Get all the trusted pubkeys given the view of an observer",
)
async def get_whitelisted_pubkeys_of_observer_endpoint(
    request: Request,
    observer_pubkey: str,
    threshold: float = Query(default=0.02, ge=0.0, le=1.0),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetWhitelistedPubkeysOfObserverResponse:

    result = await get_whitelisted_pubkeys_of_observer(db, observer_pubkey, threshold)

    result_formated = WhitelistedPubkeys(
        observerPubkey=observer_pubkey, numPubkeys=len(result), pubkeys=result
    )

    return GetWhitelistedPubkeysOfObserverResponse(data=result_formated)
