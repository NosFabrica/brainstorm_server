from fastapi import APIRouter, Depends
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_nsec import select_brainstorm_nsec_by_pubkey_on_db
from app.services.duty_menu_build import designation_rows

router = APIRouter()


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
    # The same rows the Assistant's Duty Menu offers (`duty_menu_build`).
    return designation_rows(ta_pubkey)
