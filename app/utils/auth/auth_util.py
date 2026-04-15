import hashlib
import json
from datetime import datetime, timedelta
import secrets
import string

import jwt

from app.core.config import settings
from app.utils.auth.auth_models import JWTData, JWTSQLAdminData


def create_jwt_token(
    nostr_pubkey: str,
    token_expiration_datetime: datetime,
    is_admin: bool = False,
) -> str:
    jwt_data = JWTData(
        nostr_pubkey=nostr_pubkey,
        expires_date=token_expiration_datetime,
        is_admin=is_admin,
    )

    jwt_token = jwt.encode(
        payload=json.loads(jwt_data.model_dump_json()),
        key=settings.auth_secret_key,
        algorithm=settings.auth_algorithm,
    )

    return jwt_token


def decrypt_jwt_token(jwt_token: str) -> JWTData | None:
    try:
        jwt_data = JWTData.model_validate(
            jwt.decode(
                jwt_token,
                settings.auth_secret_key,
                algorithms=[settings.auth_algorithm],
            )
        )
    except (jwt.exceptions.DecodeError, jwt.exceptions.InvalidAlgorithmError):
        return None

    return jwt_data


def encrypt_password(password: str) -> str:
    # as seen in https://docs.python.org/3/library/hashlib.html
    sha_instance = hashlib.sha256()
    sha_instance.update(str.encode(password))
    return sha_instance.hexdigest()


def sql_admin_create_jwt_token() -> str:
    jwt_data = JWTSQLAdminData(
        expires_date=datetime.now() + timedelta(minutes=15),
    )

    jwt_token = jwt.encode(
        payload=json.loads(jwt_data.model_dump_json()),
        key=settings.auth_secret_key,
        algorithm=settings.auth_algorithm,
    )

    return jwt_token


def sql_admin_decrypt_jwt_token(jwt_token: str) -> JWTSQLAdminData:
    jwt_data = JWTSQLAdminData.model_validate(
        jwt.decode(
            jwt_token,
            settings.auth_secret_key,
            algorithms=[settings.auth_algorithm],
        )
    )

    return jwt_data


def generate_secure_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
