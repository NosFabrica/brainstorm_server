from app.schemas.schemas import BrainstormPubkeyInstance, BrainstormRequestInstance
from app.services.brainstorm_request_service import create_brainstorm_request
from fastapi import APIRouter, Depends
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_nsec import select_brainstorm_nsec_by_pubkey_on_db
from app.schemas.request_response_schemas import BrainstormPubkeyResponse
from app.services.brainstorm_pubkey_service import get_or_create_brainstorm_pubkey
from app.utils.api_validators import validate_nostr_pubkey


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
    nostr_pubkey = validate_nostr_pubkey(nostr_pubkey)
    result = await get_or_create_brainstorm_pubkey(db, nostr_pubkey)
    return BrainstormPubkeyResponse(data=result)


@router.post(
    path="/{nostr_pubkey}/trigger_graperank",
    tags=[],
    dependencies=[],
    summary="trigger graperank for a specific pubkey",
)
async def trigger_brainstorm_pubkey_graperank_endpoint(
    nostr_pubkey: str,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> BrainstormPubkeyResponse:
    nsec_obj = await select_brainstorm_nsec_by_pubkey_on_db(db, pubkey=nostr_pubkey)

    nsec_pubkey = nsec_obj.pubkey
    nsec_value = nsec_obj.nsec
    nsec_created_at = nsec_obj.created_at
    nsec_updated_at = nsec_obj.updated_at

    triggered_graperank: BrainstormRequestInstance = await create_brainstorm_request(
        db=db,
        algorithm="graperank",
        parameters=nostr_pubkey,
        pubkey=nostr_pubkey,
        nsec_exists=True,
    )

    return BrainstormPubkeyResponse(
        data=BrainstormPubkeyInstance(
            global_pubkey=nsec_pubkey,
            brainstorm_pubkey=Keys.parse(secret_key=nsec_value).public_key().to_hex(),
            triggered_graperank=triggered_graperank,
            created_at=nsec_created_at,
            updated_at=nsec_updated_at,
        )
    )
