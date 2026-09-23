"""Whether a user's Policy includes support (ADR 0003). The single seam."""

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.admin_whitelist import get_whitelisted_pubkeys
from app.repos.brainstorm_nsec import get_scheduling_for_pubkey_on_db


async def is_entitled_to_support(db: AsyncDBSession, pubkey: str) -> bool:
    if pubkey in get_whitelisted_pubkeys():  # the team can always file/test
        return True
    policy = await get_scheduling_for_pubkey_on_db(db, pubkey)
    return bool(policy and policy.support_included)
