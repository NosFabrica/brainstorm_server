from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.request_response_schemas import (
    BrainstormPubkeyResponse,
)
from app.schemas.schemas import BrainstormPubkeyInstance
from app.services.brainstorm_pubkey_service import (
    brainstorm_pubkey_db_obj_to_schema_converter,
)
from app.services.brainstorm_request_service import (
    create_brainstorm_request,
)


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

    result, was_created_now = (
        await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, pubkey=nostr_pubkey
        )
    )

    brainstom_request_instance = None

    if was_created_now:
        brainstom_request_instance = await create_brainstorm_request(
            db=db,
            algorithm="graperank",
            parameters=nostr_pubkey,
            pubkey=nostr_pubkey,
            nsec_exists=True,
        )

    result_instance: BrainstormPubkeyInstance = (
        brainstorm_pubkey_db_obj_to_schema_converter(
            brainstorm_nsec_db_obj=result,
            triggered_graperank=brainstom_request_instance,
        )
    )

    return BrainstormPubkeyResponse(data=result_instance)
