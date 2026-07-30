"""NIP-05 name resolution for the /.well-known/nostr.json route."""

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.repos.brainstorm_nsec import select_all_assistant_pubkeys_on_db
from app.utils.assistant_nip05 import compute_assistant_nip05_local_part

# The reserved NIP-05 root identity — this SP's own Nostr presence.
HOUSE_IDENTITY_NAME = "_"


async def resolve_nip05_name(db: AsyncDBSession, name: str) -> dict[str, str]:
    """`{name: pubkey}` for a resolvable name, `{}` on a miss."""
    if name == HOUSE_IDENTITY_NAME:
        house_pubkey = settings.periodic_graperank_pubkey
        return {name: house_pubkey} if house_pubkey else {}

    # Uncached on purpose: a new Assistant resolves the moment its nsec row exists.
    for pubkey in await select_all_assistant_pubkeys_on_db(db):
        if compute_assistant_nip05_local_part(pubkey) == name:
            return {name: pubkey}
    return {}
