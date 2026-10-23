"""The entity a short code stands for: per-kind key rules and the on-site path."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from nostr_sdk import (
    Coordinate,
    EventId,
    Kind,
    Nip19Coordinate,
    Nip19Event,
    Nip19Profile,
    PublicKey,
)

from app.utils.nostr import to_hex_pubkey

EntityKind = Literal["profile", "event", "address", "hashtag", "tag"]

# Bounds the stored `entity_key` column.
MAX_ENTITY_KEY_LENGTH = 512

DCOSL_RELAY = "wss://dcosl.brainstorm.world"
# Never a hint: it serves search, not the entities a link points at.
SEARCH_RELAY = "wss://search.brainstorm.world"
_DLIST_KINDS = (39998, 39999)

# Any valid pubkey; only the relay is under test.
_PROBE_PUBKEY = "0" * 63 + "1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KIND = re.compile(r"^[0-9]{1,5}$")
_MAX_HASHTAG_LENGTH = 64


def _encode_uri_component(value: str) -> str:
    return quote(value, safe="-_.!~*'()")


def _hex64(value: str, what: str) -> str:
    value = value.lower()
    if not _HEX64.match(value):
        raise ValueError(f"{what} must be 64 hex characters")
    return value


def _bounded(key: str, what: str) -> str:
    if len(key) > MAX_ENTITY_KEY_LENGTH:
        raise ValueError(f"{what} is too long")
    return key


def _address(key: str) -> tuple[int, str, str]:
    parts = key.split(":", 2)
    if len(parts) != 3:
        raise ValueError("address must be <kind>:<pubkey>:<d>")
    kind, pubkey, d = parts
    if not _KIND.match(kind) or int(kind) > 65535:
        raise ValueError("address kind must be an integer 0-65535")
    return int(kind), _hex64(pubkey, "address pubkey"), d


def _tag(key: str) -> tuple[str, str]:
    author, sep, slug = key.partition(":")
    if not sep or not slug:
        raise ValueError("tag must be <author pubkey>:<slug>")
    return _hex64(author, "tag author"), slug


def _normalize_profile(key: str) -> str:
    return to_hex_pubkey(key.lower())


def _normalize_event(key: str) -> str:
    return _hex64(key, "event id")


def _normalize_address(key: str) -> str:
    kind, pubkey, d = _address(_bounded(key, "address"))
    return f"{kind}:{pubkey}:{d}"


def _normalize_hashtag(key: str) -> str:
    word = key.lower().removeprefix("#")
    if not 1 <= len(word) <= _MAX_HASHTAG_LENGTH:
        raise ValueError(f"hashtag must be 1-{_MAX_HASHTAG_LENGTH} characters")
    if "/" in word or any(c.isspace() for c in word):
        raise ValueError("hashtag must not contain '/' or whitespace")
    return word


def _normalize_tag(key: str) -> str:
    author, slug = _tag(_bounded(key, "tag"))
    return f"{author}:{slug}"


def _profile_path(key: str, relays: list[str]) -> str:
    pubkey = PublicKey.parse(key)
    if relays:
        return f"/p/{Nip19Profile(pubkey, relays).to_bech32()}"
    return f"/p/{pubkey.to_bech32()}"


def _event_path(key: str, relays: list[str]) -> str:
    return f"/e/{Nip19Event(EventId.parse(key), relays=relays).to_bech32()}"


def _address_path(key: str, relays: list[str]) -> str:
    kind, pubkey, d = _address(key)
    if not relays and kind in _DLIST_KINDS:
        relays = [DCOSL_RELAY]
    coordinate = Coordinate(Kind(kind), PublicKey.parse(pubkey), d)
    return f"/e/{Nip19Coordinate(coordinate, relays).to_bech32()}"


def _hashtag_path(key: str, relays: list[str]) -> str:
    return f"/t/{_encode_uri_component(key)}"


def _tag_path(key: str, relays: list[str]) -> str:
    author, slug = _tag(key)
    npub = PublicKey.parse(author).to_bech32()
    return f"/tags/{npub}/{_encode_uri_component(slug)}"


@dataclass(frozen=True)
class _KindRules:
    normalize: Callable[[str], str]
    path: Callable[[str, list[str]], str]


_RULES: dict[EntityKind, _KindRules] = {
    "profile": _KindRules(_normalize_profile, _profile_path),
    "event": _KindRules(_normalize_event, _event_path),
    "address": _KindRules(_normalize_address, _address_path),
    "hashtag": _KindRules(_normalize_hashtag, _hashtag_path),
    "tag": _KindRules(_normalize_tag, _tag_path),
}


def _rules_for(kind: EntityKind) -> _KindRules:
    rules = _RULES.get(kind)
    if rules is None:
        raise ValueError(f"unknown kind {kind!r}")
    return rules


def check_relay_encodable(relay: str) -> bool:
    """Whether nostr_sdk will put ``relay`` into a NIP-19 entity."""
    try:
        Nip19Profile(PublicKey.parse(_PROBE_PUBKEY), [relay]).to_bech32()
    except Exception:
        return False
    return True


def check_relay_search(relay: str) -> bool:
    return relay.strip().rstrip("/").lower() == SEARCH_RELAY


def normalize_entity_key(kind: EntityKind, key: str) -> str:
    """The stored form of ``key``; ValueError if it doesn't fit ``kind``."""
    return _rules_for(kind).normalize(key.strip())


def build_entity_path(kind: EntityKind, key: str, relays: list[str]) -> str:
    """The full on-site path for a stored ``(kind, key)`` and its stored relays."""
    hints = [
        r for r in relays if check_relay_encodable(r) and not check_relay_search(r)
    ]
    return _rules_for(kind).path(key, hints)
