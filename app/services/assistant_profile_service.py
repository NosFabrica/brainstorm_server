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
    Tag,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.loggr import loggr
from app.repos.brainstorm_nsec import select_brainstorm_nsec_by_pubkey_on_db
from app.utils.assistant_nip05 import compute_assistant_nip05

logger = loggr.get_logger(__name__)

ASSISTANT_ABOUT = (
    "I am the Brainstorm Assistant for my owner. My primary task is to publish "
    "kind 30382 Trusted Assertions so that my owner's personalized web of trust "
    "metrics are available to be utilized by any nostr client that supports NIP-85."
)

KIND_0_PUBLISH_RELAYS: list[str] = [
    "wss://relay.damus.io",
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.nostr.band",
]

# Served as static files by Brainstorm-UI (client/public/). JPEG over the WebP
# twins because not every Nostr client renders WebP avatars.
ASSISTANT_PICTURE_PATH = "/assistant-default.jpg"
ASSISTANT_BANNER_PATH = "/assistant-banner.jpg"


def _assistant_image_url(path: str) -> str | None:
    # No frontend_url → no absolute URL to hand out; omit rather than publish a
    # relative path no client can resolve.
    base = (settings.frontend_url or "").rstrip("/")
    return f"{base}{path}" if base else None


def assistant_relay_list() -> list[str]:
    """The Assistant's NIP-65 relays: the scores (TA) relay first — where its
    kind-30382s live — then the relays carrying its kind 0."""
    relays: list[str] = []
    for relay in [
        settings.nostr_upload_ta_events_relay_public_url,
        settings.trusted_list_relay,
        *KIND_0_PUBLISH_RELAYS,
    ]:
        if relay and relay not in relays:
            relays.append(relay)
    return relays


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

    # A relay hiccup on the owner-name lookup must not abort the whole kind-0 —
    # the pubkey-prefix fallback is good enough, and a later manual publish
    # (POST /user/assistantProfile) can improve the name.
    try:
        owner_name = await _fetch_owner_name(user_pubkey) or user_pubkey[:6]
    except Exception as e:
        logger.warning(f"owner kind-0 lookup failed for {user_pubkey}: {e}")
        owner_name = user_pubkey[:6]
    assistant_name = f"{owner_name}'s Brainstorm Assistant"

    keys = Keys.parse(secret_key=nsec_row.nsec)
    assistant_pubkey = keys.public_key().to_hex()

    metadata = {
        "name": assistant_name,
        "display_name": assistant_name,
        "website": settings.frontend_url,
        "about": ASSISTANT_ABOUT,
    }

    # Omitted when there's no domain — an empty claim renders as a failed badge.
    nip05 = compute_assistant_nip05(assistant_pubkey)
    if nip05:
        metadata["nip05"] = nip05

    for field, path in (
        ("picture", ASSISTANT_PICTURE_PATH),
        ("banner", ASSISTANT_BANNER_PATH),
    ):
        url = _assistant_image_url(path)
        if url:
            metadata[field] = url

    content = json.dumps(metadata)

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

        # NIP-65 relay list so outbox-model clients find the Assistant's TAs on
        # the scores relay. Best-effort: the kind 0 already went out, and the
        # kind 10040 relay hint still points clients at the TAs without it.
        try:
            relay_list = EventBuilder(kind=Kind(10002), content="").tags(
                [Tag.parse(["r", relay]) for relay in assistant_relay_list()]
            )
            relay_list_event = await client.sign_event_builder(relay_list)
            relay_output = await client.send_event(relay_list_event)
            if not relay_output.success:
                logger.warning(
                    f"assistant kind-10002 publish failed for {assistant_pubkey}: "
                    f"{relay_output.failed}"
                )
        except Exception as e:
            logger.warning(
                f"assistant kind-10002 publish failed for {assistant_pubkey}: {e}"
            )
    finally:
        await client.disconnect()

    return event.id().to_hex(), assistant_pubkey
