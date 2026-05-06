import re
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.admin_whitelist import get_whitelisted_pubkeys
from app.core.config import settings
from app.core.database import get_db
from app.repos.brainstorm_nsec import brainstorm_nsec_exists_by_pubkey_on_db
from app.utils.auth.auth_models import JWTData
from app.utils.auth.auth_util import decrypt_jwt_token
from app.utils.auth.nip98 import validate_nip98_event

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_nostr_pubkey(pubkey: str) -> str:
    normalised = pubkey.lower()
    if not _HEX64_RE.match(normalised):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid nostr pubkey: expected 64-character hex, got {len(normalised)} characters",
        )
    return normalised

# Accept both the standard Authorization: Bearer header and the legacy
# custom access_token header for backward compatibility.
auth_jwt_header = APIKeyHeader(name="access_token", scheme_name="auth_token", auto_error=False)


async def verify_token(
    request: Request,
    auth_token: Optional[str] = Security(auth_jwt_header),
    db: AsyncDBSession = Depends(get_db),
):
    auth_header = request.headers.get("authorization", "")
    auth_lower = auth_header.lower()

    # NIP-98 fallback: Authorization: Nostr <base64-encoded-signed-event>
    if auth_lower.startswith("nostr "):
        pubkey = await validate_nip98_event(request, auth_header[6:])
        if not await brainstorm_nsec_exists_by_pubkey_on_db(db, pubkey):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="NIP-98 pubkey is not registered",
            )
        is_admin = settings.admin_enabled and pubkey in get_whitelisted_pubkeys()
        # expires_date is required by JWTData but unused on this path: NIP-98
        # freshness is enforced per-request via the event's created_at ±60s
        # window, and verify_token's expires_date check runs only on the JWT
        # branch below.
        nip98_jwt_data = JWTData(
            nostr_pubkey=pubkey,
            expires_date=datetime.max,
            is_admin=is_admin,
        )
        request.state.jwt_data = nip98_jwt_data
        return nip98_jwt_data

    # JWT path: prefer standard Authorization: Bearer header; fall back to legacy access_token
    token = None
    if auth_lower.startswith("bearer "):
        token = auth_header[7:]  # strip "Bearer "
    if not token:
        token = auth_token  # legacy access_token header

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )

    jwt_data = decrypt_jwt_token(token)

    if jwt_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bad token",
        )

    if datetime.now() > jwt_data.expires_date:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your token has expired",
        )
    request.state.jwt_data = jwt_data
    return jwt_data
