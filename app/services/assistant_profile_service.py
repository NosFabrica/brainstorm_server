import json
from datetime import timedelta

from nostr_sdk import (  # type: ignore
    Client,
    EventBuilder,
    Filter,
    Keys,
    Kind,
    NostrSigner,
    PublicKey,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.loggr import loggr
from app.repos.brainstorm_nsec import select_brainstorm_nsec_by_pubkey_on_db

logger = loggr.get_logger(__name__)

ASSISTANT_ABOUT = (
    "I am the Brainstorm Assistant for my owner. My primary task is to publish "
    "kind 30382 Trusted Assertions so that my owner's personalized web of trust "
    "metrics are available to be utilized by any nostr client that supports NIP-85."
)
PROD_WEBSITE = "https://brainstorm.nosfabrica.com"
STAGING_WEBSITE = "https://brainstorm-staging.nosfabrica.com"

KIND_0_PUBLISH_RELAYS: list[str] = [
    "wss://relay.damus.io",
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.nostr.band",
]


async def _fetch_owner_name(user_pubkey: str) -> str:
    fetcher = Client()
    await fetcher.add_relay(settings.nostr_transfer_from_relay)
    await fetcher.connect()

    try:
        flt = Filter().kinds([Kind(0)]).authors([PublicKey.parse(user_pubkey)]).limit(1)
        events_obj = await fetcher.fetch_events(flt, timeout=timedelta(seconds=10))
        events = events_obj.to_vec()
    finally:
        await fetcher.disconnect()

    if not events:
        return ""

    latest = max(events, key=lambda e: e.created_at().as_secs())
    try:
        metadata = json.loads(latest.content())
    except (json.JSONDecodeError, ValueError):
        return ""

    return metadata.get("name") or metadata.get("display_name") or ""


async def publish_assistant_kind0_for_user(
    db: AsyncDBSession, user_pubkey: str
) -> tuple[str, str]:
    nsec_row = await select_brainstorm_nsec_by_pubkey_on_db(db, user_pubkey)

    owner_name = await _fetch_owner_name(user_pubkey) or user_pubkey[:6]
    assistant_name = f"{owner_name}'s Brainstorm Assistant"

    website = PROD_WEBSITE if settings.deploy_environment == "PROD" else STAGING_WEBSITE

    content = json.dumps(
        {
            "name": assistant_name,
            "display_name": assistant_name,
            "website": website,
            "about": ASSISTANT_ABOUT,
        }
    )

    keys = Keys.parse(secret_key=nsec_row.nsec)
    client = Client(signer=NostrSigner.keys(keys=keys))

    added = 0
    for relay in KIND_0_PUBLISH_RELAYS:
        try:
            await client.add_relay(relay)
            added += 1
        except Exception as e:
            logger.error(f"Failed to add relay {relay}: {e}")

    if added == 0:
        raise Exception("Failed to add any kind 0 publish relay")

    await client.connect()

    try:
        builder = EventBuilder(kind=Kind(0), content=content)
        event = await client.sign_event_builder(builder)
        output = await client.send_event(event)
        if not output.success:
            raise Exception(
                f"Failed to publish kind 0 event to any relay: {output.failed}"
            )
    finally:
        await client.disconnect()

    return event.id().to_hex(), keys.public_key().to_hex()
