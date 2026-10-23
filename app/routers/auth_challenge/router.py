from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_body_schemas import SubmitNostrAuthChallengeBody
from app.schemas.request_response_schemas import (
    NostrAuthChallenge,
    NostrAuthChallengeResponse,
    SubmitNostrAuthChallengeResponse,
)
from app.services.auth_service import (
    generate_nostr_auth_challenge,
    verify_nostr_auth_challenge,
)
from app.utils.rate_limiting.rate_limiting import (
    rate_limit_login_challenge,
    rate_limit_login_verify,
)

router = APIRouter()


@router.get(
    path="/{pubkey}",
    tags=[],
    dependencies=[Depends(rate_limit_login_challenge)],
    summary="Get a Nostr auth challenge endpoint",
)
async def get_nostr_auth_challenge_endpoint(
    pubkey: str,
) -> NostrAuthChallengeResponse:
    challenge = await generate_nostr_auth_challenge(pubkey)
    return NostrAuthChallengeResponse(data=NostrAuthChallenge(challenge=challenge))


@router.post(
    path="/{pubkey}/verify",
    tags=[],
    dependencies=[Depends(rate_limit_login_verify)],
    summary="Verify a Nostr auth challenge endpoint",
)
async def submit_nostr_auth_challenge_endpoint(
    pubkey: str,
    request: Request,
    submit_nostr_auth_challenge_body: SubmitNostrAuthChallengeBody,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> SubmitNostrAuthChallengeResponse:
    auth_token = await verify_nostr_auth_challenge(
        db, request, pubkey, submit_nostr_auth_challenge_body.signed_event
    )
    return SubmitNostrAuthChallengeResponse(data=auth_token)
