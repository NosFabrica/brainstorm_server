from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_response_schemas import BrainstormPubkeyResponse
from app.services.brainstorm_pubkey_service import get_or_create_brainstorm_pubkey


router = APIRouter()


@router.get(
    path="/{nostr_pubkey}",
    tags=[],
    dependencies=[],
    summary="Get a Branstorm Pubkey endpoint. This will get the Pubkey for the Trusted Assertions of a given Nostr Pubkey."
    + "If it doesn't exist, it is generated, and a new GrapeRank score calculation is triggered.",
)
async def get_brainstorm_pubkey_endpoint(
    nostr_pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> BrainstormPubkeyResponse:
    result = await get_or_create_brainstorm_pubkey(db, nostr_pubkey)
    return BrainstormPubkeyResponse(data=result)
