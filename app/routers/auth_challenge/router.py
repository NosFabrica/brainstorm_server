from datetime import datetime
from fastapi import HTTPException
import json
from fastapi import APIRouter, Depends, status
from app.core.database import get_db
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.request_body_schemas import SubmitNostrAuthChallengeBody
from app.schemas.request_response_schemas import (
    NostrAuthChallenge,
    NostrAuthChallengeResponse,
    SubmitNostrAuthChallengeResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from app.core.redis_db import redis_client
import secrets

from nostr_sdk import Event, TagKind


from app.services.auth_service import generate_authentication_token


CHALLENGE_TTL = 12000  # seconds (2 minutes)

router = APIRouter()


@router.get(
    path="/{pubkey}",
    tags=[],
    dependencies=[],
    summary="Get a Nostr auth challenge endpoint",
)
async def get_nostr_auth_challenge_endpoint(
    pubkey: str,
) -> NostrAuthChallengeResponse:

    challenge = secrets.token_hex(16)  # random 32-char hex string
    key = f"nostr:challenge:{pubkey}"
    await redis_client.set(key, challenge, ex=CHALLENGE_TTL)
    return NostrAuthChallengeResponse(data=NostrAuthChallenge(challenge=challenge))


@router.post(
    path="/{pubkey}/verify",
    tags=[],
    dependencies=[],
    summary="Verify a Nostr auth challenge endpoint",
)
async def submit_nostr_auth_challenge_endpoint(
    pubkey: str,
    submit_nostr_auth_challenge_body: SubmitNostrAuthChallengeBody,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> SubmitNostrAuthChallengeResponse:

    key = f"nostr:challenge:{pubkey}"
    challenge = await redis_client.get(key)

    if not challenge:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No challenge found or expired",
        )

    try:

        signed_event = Event.from_json(
            json.dumps(submit_nostr_auth_challenge_body.signed_event)
        )

        if signed_event.author().to_hex() != pubkey:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Submited event has wrong author",
            )

        # if not any( [ x for x in signed_event.tags().to_vec() if x.kind()   ] ):
        #     raise HTTPException(
        #         status_code=status.HTTP_401_UNAUTHORIZED,
        #         detail="Wrong challenge",
        #     )

        t_tags = [
            x
            for x in submit_nostr_auth_challenge_body.signed_event["tags"]
            if x[0] == "t"
        ]
        assert t_tags and t_tags[0][1] == "brainstorm_login"

        challenge_tags = [
            x
            for x in submit_nostr_auth_challenge_body.signed_event["tags"]
            if x[0] == "challenge"
        ]
        assert challenge_tags and challenge_tags[0][1] == challenge

        assert signed_event.verify_signature()

        await redis_client.delete(key)

        auth_token = generate_authentication_token(pubkey)

        await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey)

        return SubmitNostrAuthChallengeResponse(data=auth_token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid event"
        )
