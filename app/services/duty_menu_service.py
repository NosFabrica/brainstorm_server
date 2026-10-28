"""The caller's Brainstorm Assistant's Duty Menu: what it offers, and publishing it.

The menu is one kind-39999 event (`d` = `duty-menu`) signed by the Assistant's
own key — the one its Trusted Lists are signed with — saying which duties it
takes on and at which relay each is published (`duty_menu_build`). The server
signs it only when the customer asks (`POST /user/assistantDutyMenu`); when
the offer changes, the UI shows the menu as out of date and the customer
republishes. Nothing here runs on a timer. Spec: Brainstorm-UI
`docs/assistant/DUTY-MENU-ASK.md`; the § numbers are Tapestry's
`protocols/drafts/duty-menus.md`.

A publish goes where § 8.1 says: each row's relay (written at the internal TA
relay for its public URL, as scores are), the Assistant's outbox relays, every
relay an earlier version's rows named, and Nous' relay. The UI waits 30 s, so
each step has its own budget well inside that.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from nostr_sdk import (  # type: ignore
    Client,
    EventBuilder,
    Filter,
    Keys,
    Kind,
    PublicKey,
    Tag,
    Timestamp,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.schemas.schemas import (
    AssistantDutyMenuOffer,
    AssistantRelayResult,
    PublishedAssistantDutyMenu,
)
from app.services.assistant_profile_service import assistant_relay_list
from app.services.duty_menu_build import (
    DUTY_MENU_D,
    DUTY_MENU_KIND,
    NOUS_MENU_RELAYS,
    RelayTarget,
    build_menu_tags,
    earlier_relays,
    is_duty_menu,
    menu_created_at,
    offer_rows,
    publish_targets,
    relay_key,
)
from app.utils.client_tag import client_tag

logger = loggr.get_logger(__name__)

CONNECT_TIMEOUT = timedelta(seconds=4)
FETCH_TIMEOUT = timedelta(seconds=4)
# nostr-sdk gives each relay 10 s for its OK; this only bounds the call itself.
SEND_TIMEOUT_SECONDS = 12


async def _assistant_keys(db: AsyncDBSession, user_pubkey: str) -> Keys:
    nsec_row, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
        db, pubkey=user_pubkey
    )
    return Keys.parse(secret_key=nsec_row.nsec)


async def read_assistant_duty_menu_offer(
    db: AsyncDBSession, user_pubkey: str
) -> AssistantDutyMenuOffer:
    assistant = (await _assistant_keys(db, user_pubkey)).public_key().to_hex()
    return AssistantDutyMenuOffer(assistant=assistant, rows=offer_rows(assistant))


async def connect_targets(
    client: Client, targets: list[RelayTarget], failures: dict[str, str]
) -> set[str]:
    """Add and connect to `targets`; the `relay_key`s that connected. Each that
    didn't goes into `failures` with its reason."""
    for target in targets:
        try:
            await client.add_relay(target.write_url)
        except Exception as e:
            failures[relay_key(target.write_url)] = f"not a relay address: {e}"
    output = await client.try_connect(CONNECT_TIMEOUT)
    for url, reason in output.failed.items():
        failures[relay_key(url)] = reason
    return {relay_key(url) for url in output.success}


async def _fetch_earlier(
    client: Client, urls: list[str], assistant_pubkey: str
) -> list[dict[str, Any]]:
    """Every version of this Assistant's menu the relays hold (§ 8.1's filter).

    A failed read finds nothing rather than stopping the publish: the menu is
    still dated now, and replaces whatever is older.
    """
    if not urls:
        return []
    flt = (
        Filter()
        .kinds([Kind(DUTY_MENU_KIND)])
        .authors([PublicKey.parse(assistant_pubkey)])
        .identifier(DUTY_MENU_D)
    )
    try:
        events = await client.fetch_events_from(urls, flt, FETCH_TIMEOUT)
    except Exception as e:
        logger.warning(f"duty menu lookup failed for {assistant_pubkey}: {e}")
        return []
    found = [json.loads(event.as_json()) for event in events.to_vec()]
    return [
        e
        for e in found
        if e.get("pubkey") == assistant_pubkey
        and e.get("kind") == DUTY_MENU_KIND
        and any(t[:2] == ["d", DUTY_MENU_D] for t in e.get("tags") or [])
    ]


async def send_event(
    client: Client, event, urls: list[str]
) -> tuple[set[str], dict[str, str]]:
    """The `relay_key`s that took the event, and why each other one didn't."""
    if not urls:
        return set(), {}
    try:
        output = await asyncio.wait_for(
            client.send_event_to(urls, event), SEND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return set(), {relay_key(url): "timeout" for url in urls}
    except Exception as e:
        return set(), {relay_key(url): str(e) for url in urls}
    return (
        {relay_key(url) for url in output.success},
        {relay_key(url): reason for url, reason in output.failed.items()},
    )


def relay_report(
    targets: list[RelayTarget],
    accepted: set[str],
    refused: dict[str, str],
    failures: dict[str, str],
) -> list[AssistantRelayResult]:
    """One line per target, by its public address: took it, or why not."""
    report: list[AssistantRelayResult] = []
    for target in targets:
        key = relay_key(target.write_url)
        if key in accepted:
            report.append(AssistantRelayResult(url=target.url, ok=True))
        else:
            reason = refused.get(key) or failures.get(key) or "not sent"
            report.append(
                AssistantRelayResult(url=target.url, ok=False, message=reason)
            )
    return report


async def publish_assistant_duty_menu(
    db: AsyncDBSession, user_pubkey: str
) -> PublishedAssistantDutyMenu:
    keys = await _assistant_keys(db, user_pubkey)
    assistant = keys.public_key().to_hex()
    rows = offer_rows(assistant)
    row_relays = [relay for _, relay in rows]
    outbox = assistant_relay_list()

    client = Client()
    failures: dict[str, str] = {}
    try:
        # The places known before reading: where to look for earlier versions.
        known = publish_targets(row_relays, outbox, NOUS_MENU_RELAYS)
        connected = await connect_targets(client, known, failures)
        earlier = await _fetch_earlier(
            client,
            [t.write_url for t in known if relay_key(t.write_url) in connected],
            assistant,
        )

        targets = publish_targets(
            row_relays, outbox, earlier_relays(earlier), NOUS_MENU_RELAYS
        )
        named_before = [t for t in targets if t not in known]
        if named_before:
            connected |= await connect_targets(client, named_before, failures)

        builder = (
            EventBuilder(kind=Kind(DUTY_MENU_KIND), content="")
            .tags(
                [Tag.parse(t) for t in build_menu_tags(assistant, rows)]
                + [client_tag()]
            )
            .custom_created_at(
                Timestamp.from_secs(menu_created_at(int(time.time()), earlier))
            )
            # nostr-sdk drops a `p` naming the signer unless told otherwise,
            # and § 4's one `p` is the Assistant itself.
            .allow_self_tagging()
        )
        event = builder.sign_with_keys(keys)
        signed = json.loads(event.as_json())
        if not (event.verify() and is_duty_menu(signed, assistant)):
            logger.error(
                f"duty menu for {assistant} failed its own § 6.1 check: {signed}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Your Assistant's Duty Menu didn't pass its own check, "
                    "so it wasn't published."
                ),
            )

        accepted, refused = await send_event(
            client,
            event,
            [t.write_url for t in targets if relay_key(t.write_url) in connected],
        )
    finally:
        await client.disconnect()

    report = relay_report(targets, accepted, refused, failures)
    for line in report:
        if not line.ok:
            logger.warning(
                f"duty menu for {assistant} not on {line.url}: {line.message}"
            )

    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Your Assistant's Duty Menu didn't reach any relay. Try again in a moment.",
        )
    return PublishedAssistantDutyMenu(event=signed, relays=report)
