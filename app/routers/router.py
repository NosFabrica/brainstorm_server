from fastapi import APIRouter,Depends

from app.routers.brainstorm_request.router import router as brainstorm_request_router
from app.routers.brainstorm_pubkey.router import router as brainstorm_pubkey_router
from app.routers.auth_challenge.router import router as auth_challenge_router
from app.routers.user.router import router as user_router
from app.utils.api_validators import verify_token


router = APIRouter()

BRAINSTORM_PUBKEY_ROUTER_PREFIX = "/brainstormPubkey"

router.include_router(
    router=brainstorm_pubkey_router,
    prefix=BRAINSTORM_PUBKEY_ROUTER_PREFIX,
    tags=["brainstorm_pubkey"],
)

BRAINSTORM_REQUEST_ROUTER_PREFIX = "/brainstormRequest"

router.include_router(
    router=brainstorm_request_router,
    prefix=BRAINSTORM_REQUEST_ROUTER_PREFIX,
    tags=["brainstorm_request"],
)

AUTH_CHALLENGE_ROUTER_PREFIX = "/authChallenge"

router.include_router(
    router=auth_challenge_router,
    prefix=AUTH_CHALLENGE_ROUTER_PREFIX,
    tags=["auth_challenge"],
)

USER_ROUTER_PREFIX = "/user"

router.include_router( 
    dependencies=[Depends(verify_token)],  
    router=user_router,
    prefix=USER_ROUTER_PREFIX,
    tags=["user"],
) 
