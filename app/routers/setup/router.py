from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from nostr_sdk import Keys

from app.core.config import settings
from app.core.database import get_db
from app.repos.brainstorm_nsec import (
    select_brainstorm_nsec_by_pubkey_on_db,
)


router = APIRouter()

TAGS_IN_30382 = ["rank", "followers", "reporters", "muters", "hops"]

# Trusted Lists are designated by a BARE kind entry — "30392", no colon, no
# metric — which delegates the whole kind: "this provider publishes all my
# kind-30392 lists, find them at this relay". Tapestry's deployed reader parses
# `<kind>:<name>` on a 3039x as a reserved named-per-list override and treats it
# as unrecognized, so a metric-parameterised row like "30392:tag-membership"
# would be accepted by the UI and then silently ignored by every consumer.
# Convention: tapestry ADR tl-treasure-map/0001. NosFabrica/protocols does not
# cover 30392 designation at all as of 2026-09-01.
TRUSTED_LIST_KIND_ROW = "30392"


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
    relay = settings.nostr_upload_ta_events_relay_public_url
    # The TL relay if one is configured, mirroring where the service actually
    # publishes (`trusted_list_service._relay_url`). Note the asymmetry: the TA
    # relay has a separate *public* URL setting because a designation has to
    # advertise an address consumers can reach, and `trusted_list_relay` has no
    # such variant — so pointing it at an internal address would advertise an
    # unreachable relay. Unset is the safe default and the common case.
    trusted_list_relay = settings.trusted_list_relay or relay

    rows = [[f"30382:{tag}", ta_pubkey, relay] for tag in TAGS_IN_30382]
    # Same assistant key as the 30382 rows: one identity signs both this
    # Observer's Trusted Assertions and their Trusted Lists.
    rows.append([TRUSTED_LIST_KIND_ROW, ta_pubkey, trusted_list_relay])
    return rows
