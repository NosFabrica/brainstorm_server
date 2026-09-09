from datetime import datetime, timedelta

from app.core.admin_whitelist import get_whitelisted_pubkeys
from app.core.config import settings
from app.schemas.schemas import AuthSuccessfulToken
from app.utils.auth.auth_util import create_jwt_token


def generate_authentication_token(nostr_pubkey: str) -> AuthSuccessfulToken:
    token_expiration_datetime: datetime = datetime.now() + timedelta(
        minutes=settings.auth_access_token_expire_minutes
    )
    is_admin = settings.admin_enabled and nostr_pubkey in get_whitelisted_pubkeys()
    jwt_token = create_jwt_token(
        nostr_pubkey=nostr_pubkey,
        token_expiration_datetime=token_expiration_datetime,
        is_admin=is_admin,
    )

    return AuthSuccessfulToken(token=jwt_token)
