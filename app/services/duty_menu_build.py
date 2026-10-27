"""Pure rows + wire-shape logic for the Assistant's duties and its Duty Menu.

No I/O. One list of rows feeds the kind-10040 designation `GET /setup/{pubkey}`
serves, the Duty Menu the Assistant signs (kind 39999, `d` = `duty-menu`), and
the UI's Duties view. The menu's wire format is Tapestry's
`protocols/drafts/duty-menus.md` (nous-clawds4/tapestry); the § numbers below
are its. Ports `buildMenuTags` and `isDutyMenu` from Tapestry's
`src/lib/duty-menu/index.js`, which Brainstorm-UI's `lib/dutyMenu.ts` reads
with, so the three agree on what counts as a menu.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

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

# The customer's Dictionary, which the Assistant is to manage: its DList headers
# and items. Bare kinds, as the Trusted List row is.
DICTIONARY_KIND_ROWS = ["39998", "39999"]

DUTY_MENU_KIND = 39999
DUTY_MENU_D = "duty-menu"

# Nous' Duty Menu header (§ 3). The coordinate is fixed: every menu carries it,
# which is what makes a `#z` query find every menu (§ 8.2).
NOUS_PUBKEY = "15f7dafc4624b1e6b00ab7f863de1a53b71967528070ec7d1837c7a40c1c7270"
DUTY_MENU_HEADER = "39998:" + NOUS_PUBKEY + ":" + DUTY_MENU_D

# A `z` naming any author's concept header: an event carrying one is not a menu (§ 6.1).
CONCEPT_HEADER_Z = re.compile(r"^39998:[0-9a-f]{64}:concept-header$", re.IGNORECASE)

# Where Nous' header and the other Assistants' menus are (§ 8.1 item 3). Fixed
# rather than read from Nous' kind 10002: this is where Brainstorm-UI and
# Tapestry look today.
NOUS_MENU_RELAYS = ["wss://dcosl.brainstorm.world"]


def designation_rows(assistant_pubkey: str) -> list[list[str]]:
    """`GET /setup/{pubkey}`'s rows: `[key, assistant, relay]`, in this order."""
    relay = settings.nostr_upload_ta_events_relay_public_url
    # The TL relay if one is configured, mirroring where the service actually
    # publishes (`trusted_list_service._relay_url`). Note the asymmetry: the TA
    # relay has a separate *public* URL setting because a designation has to
    # advertise an address consumers can reach, and `trusted_list_relay` has no
    # such variant — so pointing it at an internal address would advertise an
    # unreachable relay. Unset is the safe default and the common case.
    trusted_list_relay = settings.trusted_list_relay or relay

    rows = [[f"30382:{tag}", assistant_pubkey, relay] for tag in TAGS_IN_30382]
    # Same assistant key as the 30382 rows: one identity signs both this
    # Observer's Trusted Assertions and their Trusted Lists.
    rows.append([TRUSTED_LIST_KIND_ROW, assistant_pubkey, trusted_list_relay])
    rows.extend(
        [kind, assistant_pubkey, settings.dictionary_relay]
        for kind in DICTIONARY_KIND_ROWS
    )
    return rows


def offer_rows(assistant_pubkey: str) -> list[list[str]]:
    """What the Duty Menu offers: `/setup`'s rows as `[key, relay]`, in its order.

    A row with no relay is left out: a `duty` row without one is invalid (§ 5.1).
    `GET /user/assistantDutyMenu` answers these and the menu carries exactly
    these, because the UI compares the two to say whether the menu is current.
    """
    return [
        [key, relay] for key, _, relay in designation_rows(assistant_pubkey) if relay
    ]


def build_menu_tags(assistant_pubkey: str, rows: list[list[str]]) -> list[list[str]]:
    """The menu's tags (§ 4): `d`, Nous' `z`, the one `p`, then a `duty` per row.

    Nothing else: no `storefront`, `not-offered`, `name` or `json`. This is a
    per-customer menu, not a storefront (§ 7).
    """
    tags = [["d", DUTY_MENU_D], ["z", DUTY_MENU_HEADER], ["p", assistant_pubkey]]
    tags.extend(["duty", key, relay] for key, relay in rows)
    return tags


def is_duty_menu(event: dict[str, Any] | None, author: str | None) -> bool:
    """§ 6.1: kind 39999, `d` = `duty-menu`, exactly one `p` and it is the
    author, Nous' header in a `z`, and no `["z", "list"]` nor a `z` naming a
    concept header. Other `z` tags are allowed."""
    if not isinstance(event, dict) or event.get("kind") != DUTY_MENU_KIND:
        return False
    tags = [t for t in event.get("tags") or [] if isinstance(t, list) and t]
    who = (author or "").lower()
    if not who or str(event.get("pubkey") or "").lower() != who:
        return False
    d = next((t for t in tags if t[0] == "d"), None)
    if d is None or len(d) < 2 or d[1] != DUTY_MENU_D:
        return False
    ps = [t for t in tags if t[0] == "p"]
    if len(ps) != 1 or len(ps[0]) < 2 or str(ps[0][1]).lower() != who:
        return False
    zs = [t[1] for t in tags if t[0] == "z" and len(t) > 1]
    if any(z == "list" or CONCEPT_HEADER_Z.match(str(z)) for z in zs):
        return False
    return DUTY_MENU_HEADER in zs


def menu_created_at(now: int, earlier: list[dict[str, Any]]) -> int:
    """At least a second after the newest version found, so a republish always
    replaces it, even one published this same second or dated ahead of us."""
    newest = max((int(e.get("created_at") or 0) for e in earlier), default=0)
    return max(now, newest + 1)


def earlier_relays(earlier: list[dict[str, Any]]) -> list[str]:
    """Every relay a `duty` row of an earlier version named, so its copy there
    is replaced (§ 8.1). Read whether or not that version is a menu."""
    relays: list[str] = []
    for event in earlier:
        for tag in event.get("tags") or []:
            if (
                isinstance(tag, list)
                and len(tag) >= 3
                and tag[0] == "duty"
                and isinstance(tag[2], str)
                and re.match(r"^wss?://", tag[2].strip(), re.IGNORECASE)
            ):
                relays.append(tag[2].strip())
    return relays


def relay_key(url: str) -> str:
    """A relay in one spelling, to tell two spellings of one relay apart from two relays."""
    return url.strip().lower().rstrip("/")


@dataclass(frozen=True)
class RelayTarget:
    url: str
    """The address the report names: the public one, as the rows give it."""
    write_url: str
    """Where the server writes: the internal TA relay for its public URL, as scores are."""


def write_url(url: str) -> str:
    if relay_key(url) == relay_key(settings.nostr_upload_ta_events_relay_public_url):
        return settings.nostr_upload_ta_events_relay
    return url


def publish_targets(*groups: list[str]) -> list[RelayTarget]:
    """Each relay once, in the order given, by its public spelling and by where it is written."""
    targets: list[RelayTarget] = []
    seen: set[str] = set()
    for group in groups:
        for url in group:
            if not url:
                continue
            target = RelayTarget(url=url, write_url=write_url(url))
            keys = {relay_key(target.url), relay_key(target.write_url)}
            if keys & seen:
                continue
            seen |= keys
            targets.append(target)
    return targets
