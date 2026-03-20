import re
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security.api_key import APIKeyHeader

from app.utils.auth.auth_util import decrypt_jwt_token

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_nostr_pubkey(pubkey: str) -> str:
    normalised = pubkey.lower()
    if not _HEX64_RE.match(normalised):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid nostr pubkey: expected 64-character hex, got {len(pubkey)} characters",
        )
    return normalised

# Accept both the standard Authorization: Bearer header and the legacy
# custom access_token header for backward compatibility.
auth_jwt_header = APIKeyHeader(name="access_token", scheme_name="auth_token", auto_error=False)


async def verify_token(
    request: Request,
    auth_token: Optional[str] = Security(auth_jwt_header),
):
    # Prefer standard Authorization: Bearer header; fall back to legacy access_token
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
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
