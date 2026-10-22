import hashlib
import secrets
import string
from datetime import timedelta

import jwt
from pydantic import ValidationError

from app.core.config import settings
from app.core.loggr import loggr
from app.utils.auth.auth_models import JWTData, TokenType
from app.utils.datetimes import utc_now

logger = loggr.get_logger(__name__)

JWT_LEEWAY = timedelta(seconds=30)
MIN_SESSION_SECRET_BYTES = 32
SQL_ADMIN_TOKEN_TTL = timedelta(minutes=15)


def warn_if_session_secret_short(secret: str) -> None:
    if len(secret.encode("utf-8")) < MIN_SESSION_SECRET_BYTES:
        logger.warning(
            "AUTH_SECRET_KEY is shorter than 32 bytes; session tokens are easier "
            "to forge. Generate one with: openssl rand -hex 32"
        )


def create_jwt_token(
    nostr_pubkey: str,
    ttl: timedelta,
    is_admin: bool = False,
) -> str:
    issued_at = utc_now().replace(microsecond=0)
    expires_at = issued_at + ttl
    return jwt.encode(
        payload={
            "sub": nostr_pubkey,
            "iat": issued_at,
            "exp": expires_at,
            "token_type": TokenType.USER.value,
            "is_admin": is_admin,
        },
        key=settings.auth_secret_key,
        algorithm=settings.auth_algorithm,
    )


def decrypt_jwt_token(jwt_token: str) -> JWTData | None:
    try:
        claims = jwt.decode(
            jwt_token,
            settings.auth_secret_key,
            algorithms=[settings.auth_algorithm],
            options={"require": ["exp", "iat", "sub"]},
            leeway=JWT_LEEWAY,
        )
        return JWTData(
            nostr_pubkey=claims["sub"],
            token_type=claims.get("token_type", TokenType.USER),
            is_admin=claims.get("is_admin", False),
        )
    except (jwt.PyJWTError, ValidationError):
        return None


def encrypt_password(password: str) -> str:
    # as seen in https://docs.python.org/3/library/hashlib.html
    sha_instance = hashlib.sha256()
    sha_instance.update(str.encode(password))
    return sha_instance.hexdigest()


def sql_admin_create_jwt_token() -> str:
    issued_at = utc_now().replace(microsecond=0)
    return jwt.encode(
        payload={"iat": issued_at, "exp": issued_at + SQL_ADMIN_TOKEN_TTL},
        key=settings.auth_secret_key,
        algorithm=settings.auth_algorithm,
    )


def sql_admin_verify_jwt_token(jwt_token: str) -> bool:
    try:
        jwt.decode(
            jwt_token,
            settings.auth_secret_key,
            algorithms=[settings.auth_algorithm],
            options={"require": ["exp", "iat"]},
            leeway=JWT_LEEWAY,
        )
    except jwt.PyJWTError:
        return False
    return True


def generate_secure_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
