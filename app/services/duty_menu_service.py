"""The caller's Brainstorm Assistant's Duty Menu: what it offers, and publishing it.

The menu is one kind-39999 event (`d` = `duty-menu`) signed by the Assistant's
own key — the one its Trusted Lists are signed with — saying which duties it
takes on and at which relay each is published (`duty_menu_build`). The server
signs it only when the customer asks (`POST /user/assistantDutyMenu`); when
the offer changes, the UI shows the menu as out of date and the customer
republishes. Nothing here runs on a timer. Spec: Brainstorm-UI
`docs/assistant/DUTY-MENU-ASK.md`; the § numbers are Tapestry's
`protocols/drafts/duty-menus.md`.

A publish first reads the earlier versions strictly, at each row's relay, the
Assistant's outbox relays and Nous' relay, and answers 502 when none of them
answers. It then goes where § 8.1 says: those relays (the internal TA relay for
its public URL, as scores are), and every relay an earlier version's rows
named. Signing and sending are `assistant_publish`'s, shared with the Treasure
Map.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.schemas.schemas import AssistantDutyMenuOffer, PublishedAssistantDutyMenu
from app.services.assistant_profile_service import assistant_relay_list
from app.services.assistant_publish import (
    assistant,
    is_own,
    reached_some,
    read_newest,
    send_to,
    sign_checked,
)
from app.services.duty_menu_build import (
    DUTY_MENU_D,
    DUTY_MENU_KIND,
    NOUS_MENU_RELAYS,
    build_menu_tags,
    earlier_relays,
    is_duty_menu,
    menu_created_at,
    offer_rows,
    publish_targets,
)

MENU = "Duty Menu"


def _is_own_menu(event: Any, assistant_pubkey: str) -> bool:
    """A version of this Assistant's menu, validly signed (§ 8.1's filter). Read
    whether or not it passes § 6.1, so a broken one is still replaced."""
    return is_own(event, assistant_pubkey, DUTY_MENU_KIND) and any(
        isinstance(t, list) and t[:2] == ["d", DUTY_MENU_D]
        for t in event.get("tags") or []
    )


async def read_assistant_duty_menu_offer(
    db: AsyncDBSession, user_pubkey: str
) -> AssistantDutyMenuOffer:
    _, keys = await assistant(db, user_pubkey)
    who = keys.public_key().to_hex()
    return AssistantDutyMenuOffer(assistant=who, rows=offer_rows(who))


async def publish_assistant_duty_menu(
    db: AsyncDBSession, user_pubkey: str
) -> PublishedAssistantDutyMenu:
    _, keys = await assistant(db, user_pubkey)
    who = keys.public_key().to_hex()
    rows = offer_rows(who)
    row_relays = [relay for _, relay in rows]
    outbox = assistant_relay_list()

    # The places known before reading: where to look for earlier versions.
    known = publish_targets(row_relays, outbox, NOUS_MENU_RELAYS)
    held = await read_newest(
        known,
        {"kinds": [DUTY_MENU_KIND], "authors": [who], "#d": [DUTY_MENU_D], "limit": 1},
        lambda e: _is_own_menu(e, who),
        MENU,
    )
    earlier = [e for e in held.values() if e is not None]
    targets = publish_targets(
        row_relays, outbox, earlier_relays(earlier), NOUS_MENU_RELAYS
    )

    event, signed = sign_checked(
        keys,
        DUTY_MENU_KIND,
        "",
        build_menu_tags(who, rows),
        menu_created_at(int(time.time()), earlier),
        lambda s: is_duty_menu(s, who),
        MENU,
        # § 4's one `p` is the Assistant itself.
        self_tagging=True,
    )
    report = reached_some(await send_to(event, targets, MENU, who), MENU)
    return PublishedAssistantDutyMenu(event=signed, relays=report)
