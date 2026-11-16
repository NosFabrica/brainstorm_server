"""Parsing of kind-39999 tag elements and taggings.

Pure functions over event dicts — no I/O, no DB. The ingest branch in
`process_strfry_event` and the tests both call these directly.

Wire contract (tapestry `protocols/drafts/tags.md`, and the deployed
publishers): tag elements and taggings are BOTH kind 39999, distinguished
only by which concept their `z` tag names.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeGuard

from app.utils.nostr import tag_values

# --------------------------------------------------------------------------
# The z-tag composition pubkey. THIS IS NOT A SIGNING KEY and is NOT this
# deployment's assistant pubkey.
#
# It is tapestry's `LEGACY_Z_TAG_PUBKEY` (src/api/profile-tags/index.js:49).
# Historical kind-39999 events on EVERY Brainstorm/Tapestry deployment compose
# their z-tags with this literal, including events that predate any deployment
# noticing it didn't match the on-disk TA. It is wire-binding: changing it
# orphans historical data across all deployments — that is the d3a2640a
# "lost tags" incident, recorded in tapestry ADR 0015.
#
# Use it ONLY for z-tag composition. For anything meaning "the TA pubkey"
# (signing, author filtering), use the observer's own assistant key.
# --------------------------------------------------------------------------
LEGACY_Z_TAG_PUBKEY = "82b75e474dda005e912bcbb910391c60c2b89cc7faf5d3c30b7c59a324973833"

TAG_Z_TAG = "39998:" + LEGACY_Z_TAG_PUBKEY + ":tag"
NOSTR_USER_TAG_Z_TAG = "39998:" + LEGACY_Z_TAG_PUBKEY + ":nostr-user-tag"

TAGGING_KIND = 39999

# Polarity bucketing (tags.md "Polarity"): >= 0.5 applied, <= -0.5 disputed,
# the open interval between is RESERVED for a future graded-valence arc and is
# counted in neither bucket. An absent polarity tag means applied.
APPLY_THRESHOLD = 0.5
DISPUTE_THRESHOLD = -0.5

_HEX64 = 64


@dataclass(frozen=True)
class TagElement:
    event_id: str
    author_pubkey: str
    slug: str
    name: str
    description: str
    created_at_unix: int


@dataclass(frozen=True)
class UserTagging:
    event_id: str
    asserter_pubkey: str
    d_tag: str
    target_pubkey: str
    tag_event_id: str
    polarity: float
    created_at_unix: int


def _is_hex64(value: object) -> TypeGuard[str]:
    """TypeGuard, not plain bool, so callers narrow Optional to str."""
    if not isinstance(value, str) or len(value) != _HEX64:
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


def _first_tag_value(event: dict, key: str) -> str | None:
    """First non-empty string value; malformed tags are skipped, not fatal."""
    for value in tag_values(event.get("tags") or [], key):
        if isinstance(value, str) and value:
            return value
    return None


def z_tag_of(event: dict) -> str | None:
    """The event's first `z` tag value, or None."""
    return _first_tag_value(event, "z")


def read_polarity(event: dict) -> float:
    """Polarity as a float. Absent or unparseable -> 1.0 (applied)."""
    raw = _first_tag_value(event, "polarity")
    if raw is None:
        return 1.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 1.0


def is_applied(polarity: float) -> bool:
    return polarity >= APPLY_THRESHOLD


def is_disputed(polarity: float) -> bool:
    return polarity <= DISPUTE_THRESHOLD


def is_neutral(polarity: float) -> bool:
    """The reserved open interval — counted in neither bucket."""
    return not is_applied(polarity) and not is_disputed(polarity)


def parse_tag_element(event: dict) -> TagElement | None:
    """A kind-39999 tag element, or None if it isn't one / is malformed.

    The payload is JSON in `content` (tapestry also accepts a `json` tag; both
    are read here). `slug` is required — an element without one is unusable.
    """
    if z_tag_of(event) != TAG_Z_TAG:
        return None
    event_id = event.get("id")
    author = event.get("pubkey")
    if not _is_hex64(event_id) or not _is_hex64(author):
        return None

    raw = _first_tag_value(event, "json") or event.get("content") or ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag")
    if not isinstance(tag, dict):
        return None

    slug = tag.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        return None

    name = tag.get("name")
    description = tag.get("description")
    return TagElement(
        event_id=event_id,
        author_pubkey=author,
        slug=slug,
        # Name falls back to the slug so a TL always has a usable title.
        name=name if isinstance(name, str) and name.strip() else slug,
        description=description if isinstance(description, str) else "",
        created_at_unix=int(event.get("created_at") or 0),
    )


def parse_user_tagging(event: dict) -> UserTagging | None:
    """A kind-39999 tagging, or None if it isn't one / is malformed.

    Requires `p` (target) and `e` (the tag element's event id). The deployed
    publishers reference the element by `e`, not `a` — see tags.md's
    "Deployed variant" note; an `a`-only future variant would need a union read.
    """
    if z_tag_of(event) != NOSTR_USER_TAG_Z_TAG:
        return None
    event_id = event.get("id")
    asserter = event.get("pubkey")
    if not _is_hex64(event_id) or not _is_hex64(asserter):
        return None

    target = _first_tag_value(event, "p")
    tag_event_id = _first_tag_value(event, "e")
    if not _is_hex64(target) or not _is_hex64(tag_event_id):
        return None

    # The deterministic d-tag is the replaceability key. Absent one, fall back
    # to the natural identity so the assertion still collapses correctly rather
    # than accumulating a row per republish.
    d_tag = _first_tag_value(event, "d") or (target + ":" + tag_event_id)

    return UserTagging(
        event_id=event_id,
        asserter_pubkey=asserter,
        d_tag=d_tag,
        target_pubkey=target,
        tag_event_id=tag_event_id,
        polarity=read_polarity(event),
        created_at_unix=int(event.get("created_at") or 0),
    )


# --------------------------------------------------------------------------
# Event taggings (ADR `trusted-lists/0002` D6).
#
# A tagging of a note, article, listing or DList item doesn't name its Tag
# directly. It names a per-Tag *tagging header* in a descriptor `z`
# (`39999:<headerAuthor>:tagging:<slug>-tagging`), and the header's `a` names
# the Tag element. Both are kind 39999 and are told apart by a concept `z`,
# `39998:<namespace>:<concept>`, for one of the honored namespaces.
# --------------------------------------------------------------------------
TAGGING_HEADER_CONCEPT = "tagging-with-specific-tag"
EVENT_TAG_CONCEPT = "nostr-event-tag"

_COORDINATE = re.compile(r"^([0-9]+):([0-9a-f]{64}):(.*)$")
TAG_ELEMENT_KIND = 39999


@dataclass(frozen=True)
class Coordinate:
    kind: int
    pubkey: str
    d_tag: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.pubkey}:{self.d_tag}"


def parse_coordinate(value: object) -> Coordinate | None:
    """`<kind>:<64-hex>:<d>` for an addressable kind, or None.

    Split on the first two colons only: a `d` may itself hold colons (an
    article whose `d` is a URL).
    """
    if not isinstance(value, str):
        return None
    match = _COORDINATE.match(value)
    if match is None:
        return None
    kind = int(match.group(1))
    if not 30000 <= kind < 40000:
        return None
    return Coordinate(kind, match.group(2), match.group(3))


@dataclass(frozen=True)
class TaggingHeader:
    event_id: str
    author_pubkey: str
    d_tag: str
    # The Tag element, by coordinate (`a`, what the client writes) or by event
    # id (`e`). Exactly one of the two is set.
    tag_author_pubkey: str | None
    tag_slug: str | None
    tag_event_id: str | None
    created_at_unix: int

    @property
    def coordinate(self) -> str:
        return f"{TAGGING_KIND}:{self.author_pubkey}:{self.d_tag}"


@dataclass(frozen=True)
class EventTagging:
    event_id: str
    asserter_pubkey: str
    d_tag: str
    # An event id (slot "e") or an addressable coordinate (slot "a").
    target: str
    target_slot: str
    # The relay hint on the target tag, if any; a lookup tries it after the
    # configured relays (D6).
    target_relay: str | None
    header_coordinate: str
    polarity: float
    created_at_unix: int


def _honored(honored: Iterable[str] | None) -> tuple[str, ...]:
    if honored is not None:
        return tuple(honored)
    from app.core.config import settings

    return tuple(settings.tagging_honored_namespaces)


def joins_concept(
    event: dict, concept: str, honored: Iterable[str] | None = None
) -> bool:
    """Whether any of the event's `z` tags is `39998:<ns>:<concept>` for an
    honored namespace. A tagging counts if it joins any of them."""
    handles = {f"39998:{ns}:{concept}" for ns in _honored(honored)}
    return any(
        isinstance(value, str) and value in handles
        for value in tag_values(event.get("tags") or [], "z")
    )


def _single(event: dict, key: str) -> list | None:
    """The one `key` tag (the whole tag), or None if there are none or several."""
    found = [
        t
        for t in event.get("tags") or []
        if isinstance(t, list) and len(t) >= 2 and t[0] == key
    ]
    return found[0] if len(found) == 1 else None


def parse_tagging_header(
    event: dict, honored: Iterable[str] | None = None
) -> TaggingHeader | None:
    """A kind-39999 tagging header, or None if it isn't one / is malformed."""
    if event.get("kind") != TAGGING_KIND:
        return None
    if not joins_concept(event, TAGGING_HEADER_CONCEPT, honored):
        return None
    event_id = event.get("id")
    author = event.get("pubkey")
    if not _is_hex64(event_id) or not _is_hex64(author):
        return None
    d_tag = _first_tag_value(event, "d")
    if d_tag is None:
        return None

    tag_author = tag_slug = tag_event_id = None
    coordinate = parse_coordinate(_first_tag_value(event, "a"))
    if coordinate is not None:
        if coordinate.kind != TAG_ELEMENT_KIND or not coordinate.d_tag:
            return None
        tag_author, tag_slug = coordinate.pubkey, coordinate.d_tag
    else:
        element_id = _first_tag_value(event, "e")
        if not _is_hex64(element_id):
            return None
        tag_event_id = element_id.lower()

    return TaggingHeader(
        event_id=event_id,
        author_pubkey=author,
        d_tag=d_tag,
        tag_author_pubkey=tag_author,
        tag_slug=tag_slug,
        tag_event_id=tag_event_id,
        created_at_unix=int(event.get("created_at") or 0),
    )


def parse_event_tagging(
    event: dict, honored: Iterable[str] | None = None
) -> EventTagging | None:
    """A kind-39999 event tagging, or None if it isn't one / is malformed.

    Needs a descriptor `z` naming a tagging header's coordinate and exactly one
    target: an `a` coordinate or an `e` id, never both.
    """
    if event.get("kind") != TAGGING_KIND:
        return None
    if not joins_concept(event, EVENT_TAG_CONCEPT, honored):
        return None
    event_id = event.get("id")
    asserter = event.get("pubkey")
    if not _is_hex64(event_id) or not _is_hex64(asserter):
        return None

    descriptor = next(
        (
            c
            for c in (
                parse_coordinate(v) for v in tag_values(event.get("tags") or [], "z")
            )
            if c is not None and c.kind == TAGGING_KIND and c.d_tag
        ),
        None,
    )
    if descriptor is None:
        return None

    a_tag, e_tag = _single(event, "a"), _single(event, "e")
    has_a = any(isinstance(t, list) and t[:1] == ["a"] for t in event.get("tags") or [])
    has_e = any(isinstance(t, list) and t[:1] == ["e"] for t in event.get("tags") or [])
    if has_a and has_e:
        return None
    if a_tag is not None:
        if parse_coordinate(a_tag[1]) is None:
            return None
        target, slot = str(a_tag[1]), "a"
    elif e_tag is not None:
        if not _is_hex64(e_tag[1]):
            return None
        target, slot = e_tag[1].lower(), "e"
    else:
        return None
    hint = (a_tag or e_tag or [])[2:3]
    relay = hint[0] if hint and isinstance(hint[0], str) and hint[0] else None

    d_tag = _first_tag_value(event, "d") or (str(descriptor) + ":" + target)
    return EventTagging(
        event_id=event_id,
        asserter_pubkey=asserter,
        d_tag=d_tag,
        target=target,
        target_slot=slot,
        target_relay=relay,
        header_coordinate=str(descriptor),
        polarity=read_polarity(event),
        created_at_unix=int(event.get("created_at") or 0),
    )
