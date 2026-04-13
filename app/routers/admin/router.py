from typing import Optional

from app.utils.auth.auth_models import JWTData
from fastapi import APIRouter, Depends, HTTPException, Request, status

from nostr_sdk import PublicKey

from app.core.config import settings
from app.core.loggr import loggr
from app.routers.admin.nsec_encryption.router import router as nsec_encryption_router
from app.routers.brainstorm_pubkey.router import router as brainstorm_pubkey_router
from app.utils.api_validators import verify_token

logger = loggr.get_logger(__name__)

_whitelisted_pubkeys: set[str] = set()


def _normalize_pubkey(raw: str) -> str:
    if raw.startswith("npub1"):
        return PublicKey.parse(raw).to_hex()
    return raw


def init_admin_whitelist() -> None:
    global _whitelisted_pubkeys
    raw = settings.admin_whitelisted_pubkeys
    if not raw:
        _whitelisted_pubkeys = set()
    else:
        _whitelisted_pubkeys = {
            _normalize_pubkey(p.strip()) for p in raw.split(",") if p.strip()
        }

    if settings.admin_enabled:
        truncated = [
            PublicKey.parse(pk).to_bech32()[:16] + "..." for pk in _whitelisted_pubkeys
        ]
        logger.info(
            f"Admin routes ENABLED. Whitelisted pubkeys ({len(truncated)}): {truncated}"
        )
    else:
        logger.info("Admin routes DISABLED.")


def get_whitelisted_pubkeys() -> set[str]:
    return _whitelisted_pubkeys


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
