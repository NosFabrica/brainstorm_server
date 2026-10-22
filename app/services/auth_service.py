import json
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request, status
from nostr_sdk import Event
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.admin_whitelist import get_whitelisted_pubkeys
from app.core.config import settings
from app.core.redis_db import redis_client
from app.repos.brainstorm_nsec import (
    brainstorm_nsec_exists_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.schemas import AuthSuccessfulToken
from app.services.session_service import current_session_version
from app.utils.auth.auth_util import create_jwt_token
from app.utils.nostr import first_tag_value
from app.utils.rate_limiting.rate_limiting import rate_limit_login_new_observer

CHALLENGE_TTL = 300  # seconds
LOGIN_KIND = 22242


def _challenge_key(nonce: str) -> str:
    return f"nostr:challenge:{nonce}"


def _unauthorized(detail: str = "Invalid event") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def generate_authentication_token(
    nostr_pubkey: str, session_version: int = 0
) -> AuthSuccessfulToken:
    is_admin = settings.admin_enabled and nostr_pubkey in get_whitelisted_pubkeys()
    jwt_token = create_jwt_token(
        nostr_pubkey=nostr_pubkey,
        ttl=timedelta(minutes=settings.auth_access_token_expire_minutes),
        is_admin=is_admin,
        session_version=session_version,
    )

    return AuthSuccessfulToken(token=jwt_token)


async def generate_nostr_auth_challenge(pubkey: str) -> str:
    challenge = secrets.token_hex(16)
    await redis_client.set(_challenge_key(challenge), pubkey, ex=CHALLENGE_TTL)
    return challenge


async def verify_nostr_auth_challenge(
    db: AsyncDBSession, request: Request, pubkey: str, raw_event: dict
) -> AuthSuccessfulToken:
    """Consume the challenge the signed login event answers, then mint a session token."""
    try:
        signed_event = Event.from_json(json.dumps(raw_event))
    except Exception:
        raise _unauthorized()

    if signed_event.kind().as_u16() != LOGIN_KIND:
        raise _unauthorized()
    if signed_event.author().to_hex() != pubkey:
        raise _unauthorized("Submited event has wrong author")
    tags = raw_event.get("tags") or []
    if first_tag_value(tags, "t") != "brainstorm_login":
        raise _unauthorized()
    challenge = first_tag_value(tags, "challenge")
    if not challenge:
        raise _unauthorized()
    if not signed_event.verify():
        raise _unauthorized()

    issued_for = await redis_client.getdel(_challenge_key(challenge))
    if issued_for is None or issued_for != pubkey:
        raise _unauthorized("No challenge found or expired")

    if not await brainstorm_nsec_exists_by_pubkey_on_db(db, pubkey):
        await rate_limit_login_new_observer(request)

    await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey)
    return generate_authentication_token(
        pubkey, session_version=await current_session_version(db, pubkey)
    )
