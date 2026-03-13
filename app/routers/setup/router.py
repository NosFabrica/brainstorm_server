from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from nostr_sdk import Keys

from app.core.config import settings
from app.core.database import get_db
from app.repos.brainstorm_nsec import (
    select_brainstorm_nsec_by_pubkey_on_db,
)


router = APIRouter()

TAGS_IN_30382 = ["rank", "followers"]


@router.get(
    path="/{nostr_pubkey}",
    summary="Returns the setup information for a given Nostr Pubkey, "
    "including which 30382 tags are served and where to find them.",
)
async def get_setup_endpoint(
    nostr_pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> list[list[str]]:
    brainstorm_nsec = await select_brainstorm_nsec_by_pubkey_on_db(
        db, pubkey=nostr_pubkey
    )

    ta_pubkey = Keys.parse(secret_key=brainstorm_nsec.nsec).public_key().to_hex()
    relay = settings.nostr_upload_ta_events_relay

    return [
        [f"30382:{tag}", ta_pubkey, relay]
        for tag in TAGS_IN_30382
    ]
