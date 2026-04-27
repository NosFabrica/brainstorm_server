from app.utils.auth.auth_models import JWTData
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.admin_whitelist import get_whitelisted_pubkeys
from app.core.config import settings
from app.core.loggr import loggr
from app.routers.admin.activity.router import router as activity_router
from app.routers.admin.graperank.router import router as graperank_router
from app.routers.admin.nsec_encryption.router import router as nsec_encryption_router
from app.routers.admin.stats.router import router as stats_router
from app.routers.admin.users.router import router as users_router
from app.routers.brainstorm_pubkey.router import router as brainstorm_pubkey_router
from app.routers.brainstorm_request.router import router as brainstorm_request_router
from app.utils.api_validators import verify_token

logger = loggr.get_logger(__name__)


async def verify_admin_access(
    request: Request,
):
    if not settings.admin_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin routes are disabled",
        )

    whitelist = get_whitelisted_pubkeys()
    if not whitelist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No whitelisted pubkeys configured",
        )

    jwt_data: JWTData = request.state.jwt_data
    if jwt_data.nostr_pubkey not in whitelist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access admin routes",
        )


router = APIRouter(dependencies=[Depends(verify_token), Depends(verify_admin_access)])

router.include_router(
    router=brainstorm_pubkey_router,
    prefix="/brainstormPubkey",
    tags=["admin"],
)

router.include_router(
    router=nsec_encryption_router,
    prefix="/nsec-encryption",
    tags=["admin"],
)

router.include_router(
    router=users_router,
    prefix="/users",
    tags=["admin"],
)

router.include_router(
    router=activity_router,
    prefix="/activity",
    tags=["admin"],
)

router.include_router(
    router=stats_router,
    prefix="/stats",
    tags=["admin"],
)

router.include_router(
    router=brainstorm_request_router,
    prefix="/brainstormRequest",
    tags=["admin"],
)

router.include_router(
    router=graperank_router,
    prefix="/graperank",
    tags=["admin"],
)
