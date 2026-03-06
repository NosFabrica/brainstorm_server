from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings


from app.schemas.schemas import AuthSuccessfulToken
from app.utils.auth.auth_util import create_jwt_token, encrypt_password


def generate_authentication_token(nostr_pubkey: str) -> AuthSuccessfulToken:
    token_expiration_datetime: datetime = datetime.now() + timedelta(
        minutes=settings.auth_access_token_expire_minutes
    )
    jwt_token = create_jwt_token(
        nostr_pubkey=nostr_pubkey,
        token_expiration_datetime=token_expiration_datetime,
    )

    return AuthSuccessfulToken(token=jwt_token)
