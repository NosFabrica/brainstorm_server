"""**Pure, no I/O.** The Assistant's AI keys: its NIP-FA list (kind 12000).

The list's private tags are a JSON array of string arrays, encrypted by the
Assistant to itself; a `key` tag is `["key", <key>, <url>]`. The owner edits the
whole array (read-modify-write in Brainstorm-UI, keeping tags it doesn't
understand); the server checks the shape and NIP-FA's URL rule before signing.

Every error here is a fixed sentence naming a tag by position, never by value:
the values are AI keys.
"""
from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

AGENT_KEYS_KIND = 12000
# More than anyone keeps; the bound only keeps the list readable.
MAX_TAGS = 32


class AgentKeysRejected(ValueError):
    """The decrypted list isn't one the Assistant can sign. Fixed message, no value."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_key_url_allowed(key: str, url: str) -> bool:
    """NIP-FA's rule: the URL MUST be `https`, unless the key is empty or the
    host is loopback (`localhost`, `127.0.0.0/8`, `::1`), when `http` is allowed too."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if not host:
        return False
    scheme = parts.scheme.lower()
    if scheme == "https":
        return True
    return scheme == "http" and (key == "" or _is_loopback(host))


def parse_tag_array(plaintext: str) -> list[list[str]]:
    """The plaintext as a tag array: a JSON array of non-empty string arrays.
    Raises `AgentKeysRejected` otherwise. Size and URL rules are `check_new_list`'s:
    a list read back is taken as it is, as NIP-FA's readers do."""
    try:
        tags: Any = json.loads(plaintext)
    except (ValueError, TypeError):
        raise AgentKeysRejected("The list isn't JSON.") from None
    if not isinstance(tags, list):
        raise AgentKeysRejected("The list isn't a JSON array.")
    for i, tag in enumerate(tags):
        if (
            not isinstance(tag, list)
            or not tag
            or not all(isinstance(v, str) for v in tag)
        ):
            raise AgentKeysRejected(f"Tag {i + 1} isn't an array of strings.")
    return tags


def check_new_list(plaintext: str) -> list[list[str]]:
    """A new list the owner sent: a tag array of at most `MAX_TAGS` tags, each
    `key` tag carrying a URL NIP-FA allows."""
    tags = parse_tag_array(plaintext)
    if len(tags) > MAX_TAGS:
        raise AgentKeysRejected(f"The list has more than {MAX_TAGS} tags.")
    for i, tag in enumerate(tags):
        if tag[0] != "key":
            continue
        if len(tag) < 3 or not is_key_url_allowed(tag[1], tag[2]):
            raise AgentKeysRejected(
                f"Key {i + 1} needs an https URL (http only for loopback or an empty key)."
            )
    return tags


def serialize(tags: list[list[str]]) -> str:
    """The plaintext the Assistant encrypts to itself. `[]` for an empty list,
    since NIP-44 can't encrypt an empty string."""
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":"))


def newest(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest of the versions read: the latest `created_at`, then the lowest
    id (NIP-01's tie-break for replaceable events)."""
    best: dict[str, Any] | None = None
    for event in events:
        if best is None:
            best = event
            continue
        at, best_at = int(event.get("created_at", 0)), int(best.get("created_at", 0))
        if at > best_at or (at == best_at and event.get("id", "") < best.get("id", "")):
            best = event
    return best


def next_created_at(now: int, previous: int | None) -> int:
    """NIP-FA: max(now, previous + 1)."""
    return now if previous is None else max(now, previous + 1)


def has_quorum(answered: int, asked: int) -> bool:
    """NIP-FA: more than half of the write relays must answer the read."""
    return asked > 0 and answered * 2 > asked
