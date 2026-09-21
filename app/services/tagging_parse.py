"""Parsing of kind-39999 tag elements and taggings.

Pure functions over event dicts — no I/O, no DB. The ingest branch in
`process_strfry_event` and the tests both call these directly.

Wire contract (tapestry `protocols/drafts/tags.md`, and the deployed
publishers): tag elements and taggings are BOTH kind 39999, distinguished
only by which concept their `z` tag names.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeGuard

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
    for tag in event.get("tags") or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == key:
            value = tag[1]
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
