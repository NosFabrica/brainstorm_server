"""NIP-05 name resolution for the /.well-known/nostr.json route."""

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.repos.brainstorm_nsec import select_all_assistant_pubkeys_on_db
from app.utils.assistant_nip05 import compute_assistant_nip05_local_part

# The reserved NIP-05 root identity — this SP's own Nostr presence.
HOUSE_IDENTITY_NAME = "_"


async def _resolve_names(db: AsyncDBSession, name: str) -> dict[str, str]:
    if name == HOUSE_IDENTITY_NAME:
        house_pubkey = settings.periodic_graperank_pubkey
        return {name: house_pubkey} if house_pubkey else {}

    # Uncached on purpose: a new Assistant resolves the moment its nsec row exists.
    for pubkey in await select_all_assistant_pubkeys_on_db(db):
        if compute_assistant_nip05_local_part(pubkey) == name:
            return {name: pubkey}
    return {}


async def build_nip05_document(db: AsyncDBSession, name: str) -> dict:
    """The NIP-05 response body for `name` — `{"names": {}}` on a miss.

    NIP-05 keys the recommended `relays` attribute by pubkey, and says a server
    generating responses dynamically SHOULD serve relays for any name it serves.
    Ours is where the Assistant's kind-30382 Trusted Assertions actually live.
    """
    names = await _resolve_names(db, name)
    if not names:
        return {"names": {}}

    document: dict = {"names": names}
    ta_relay = settings.nostr_upload_ta_events_relay_public_url
    if ta_relay:
        document["relays"] = {pubkey: [ta_relay] for pubkey in names.values()}
    return document
