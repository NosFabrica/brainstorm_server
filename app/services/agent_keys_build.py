"""**Pure, no I/O.** The Assistant's AI keys: its NIP-FA list (kind 12000).

The list's private tags are a JSON array of string arrays, encrypted by the
Assistant to itself; a `key` tag is `["key", <key>, <url>]`. The owner edits the
whole array (read-modify-write in Brainstorm-UI, keeping tags it doesn't
understand); the server checks the shape and NIP-FA's URL rule before signing.

Every error here is a fixed sentence naming a tag by position, never by value:
the values are AI keys.
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any

AGENT_KEYS_KIND = 12000
# More than anyone keeps; the bound only keeps the list readable.
MAX_TAGS = 32


class AgentKeysRejected(ValueError):
    """The decrypted list isn't one the Assistant can sign. Fixed message, no value."""


# Quartz's `AgentKeyTag.isUrlAllowed`, ported exactly (quartz
# nipFAAgentKeys/tags/AgentKeyTag.kt), so a URL the apps refuse is refused here
# too, and the other way round. Kotlin's `Char.isWhitespace`: the ASCII
# separators \t..\r and \x1c..\x1f, and Unicode's space, line and paragraph
# separators.
def _is_whitespace(c: str) -> bool:
    return (
        "\t" <= c <= "\r"
        or "\x1c" <= c <= "\x1f"
        or unicodedata.category(c) in ("Zs", "Zl", "Zp")
    )


def _trim(text: str) -> str:
    start, end = 0, len(text)
    while start < end and _is_whitespace(text[start]):
        start += 1
    while end > start and _is_whitespace(text[end - 1]):
        end -= 1
    return text[start:end]


def _authority(url: str) -> str | None:
    if "://" not in url:
        return None
    authority = url.split("://", 1)[1]
    for stop in "/?#":
        authority = authority.split(stop, 1)[0]
    return authority or None


def _is_dotted_127(host: str) -> bool:
    octets = host.split(".")
    return (
        len(octets) == 4
        and octets[0] == "127"
        and all(
            0 < len(part) <= 3
            and all("0" <= c <= "9" for c in part)
            and int(part) <= 255
            for part in octets
        )
    )


def _is_loopback_authority(authority: str) -> bool:
    """`localhost`, `[::1]` or a dotted `127.0.0.0/8` address, and no userinfo:
    `http://localhost@evil.example` is evil.example, and
    `http://evil.example@localhost` is refused all the same. Nothing else: not
    `[0:0:0:0:0:0:0:1]`, not `127.1`, as Quartz."""
    if "@" in authority:
        return False
    if authority.startswith("["):
        host = authority.split("]", 1)[0] + "]" if "]" in authority else "]"
    else:
        host = authority.split(":", 1)[0]
    host = host.lower()
    return host in ("localhost", "[::1]") or _is_dotted_127(host)


def is_key_url_allowed(key: str, url: str) -> bool:
    """NIP-FA's rule, as Quartz writes it: no whitespace or control character
    inside the (trimmed) URL, and `https`, or `http` when the key is empty or
    the host is loopback."""
    trimmed = _trim(url)
    if not trimmed or any(_is_whitespace(c) or ord(c) < 0x20 for c in trimmed):
        return False
    scheme = trimmed.split("://", 1)[0].lower() if "://" in trimmed else ""
    authority = _authority(trimmed)
    if authority is None:
        return False
    if scheme == "https":
        return True
    return scheme == "http" and (key == "" or _is_loopback_authority(authority))


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


def is_expected_version(
    current: dict[str, Any] | None,
    previous_id: str | None,
    previous_created_at: int | None,
) -> bool:
    """Is `current`, the newest version read now, the one the owner edited?
    None and None mean they edited none; otherwise each one given must match,
    the id being the stronger: two versions can share a `created_at`."""
    if current is None:
        return previous_id is None and previous_created_at is None
    if previous_id is None and previous_created_at is None:
        return False
    if previous_id is not None and current.get("id") != previous_id:
        return False
    return previous_created_at is None or (
        current.get("created_at") == previous_created_at
    )


def next_created_at(now: int, previous: int | None) -> int:
    """NIP-FA: max(now, previous + 1)."""
    return now if previous is None else max(now, previous + 1)


def has_quorum(answered: int, asked: int) -> bool:
    """NIP-FA: more than half of the write relays must answer the read."""
    return asked > 0 and answered * 2 > asked
