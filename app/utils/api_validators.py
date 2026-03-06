from datetime import datetime

from fastapi import HTTPException, Request, Security, status
from fastapi.security.api_key import APIKeyHeader

from app.utils.auth.auth_util import decrypt_jwt_token

auth_jwt_header = APIKeyHeader(name="access_token", scheme_name="auth_token")


async def verify_token(request: Request, auth_token: str = Security(auth_jwt_header)):
    jwt_data = decrypt_jwt_token(auth_token)

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
