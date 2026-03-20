from fastapi import HTTPException
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.schemas import BrainstormPubkeyInstance
from app.services.brainstorm_request_service import create_brainstorm_request

logger = loggr.get_logger(__name__)


async def get_or_create_brainstorm_pubkey(
    db: AsyncDBSession, nostr_pubkey: str
) -> BrainstormPubkeyInstance:
    result, was_created_now = (
        await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, pubkey=nostr_pubkey
        )
    )

    # Read ORM attributes eagerly before any further DB ops expire the object
    result_pubkey = result.pubkey
    result_nsec = result.nsec
    result_created_at = result.created_at
    result_updated_at = result.updated_at

    triggered_graperank = None
    if was_created_now:
        try:
            triggered_graperank = await create_brainstorm_request(
                db=db,
                algorithm="graperank",
                parameters=nostr_pubkey,
                pubkey=nostr_pubkey,
                nsec_exists=True,
            )
        except HTTPException:
            raise  # preserve downstream HTTP status
        except Exception as exc:
            # Observer is created; GrapeRank is re-triggerable, don't fail the request.
            logger.error(
                f"Failed to auto-trigger GrapeRank for {nostr_pubkey}: {exc}"
            )

    return BrainstormPubkeyInstance(
        global_pubkey=result_pubkey,
        brainstorm_pubkey=Keys.parse(secret_key=result_nsec).public_key().to_hex(),
        triggered_graperank=triggered_graperank,
        created_at=result_created_at,
        updated_at=result_updated_at,
    )
