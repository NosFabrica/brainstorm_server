"""The Assistant's concept copies, as data: their tags, what a press does given
what the Assistant already holds, and their date. Pure, no I/O
(`assistant_dictionary_service` does the reading, signing and sending).

A concept is in someone's Dictionary when they hold a local copy of it: a
kind-39998 header at the community header's `d`, pointing back with
`["b", <community coordinate>, "pointer"]`. The Assistant's copy repeats the
community's definition verbatim, the shape Tapestry's header contract gives
an Assistant's copy (`protocols/drafts/assistant-designation.md`); withdrawing
republishes it with `["b", "b-tag-deferred"]`, "considered, affiliated with
none", never a NIP-09 deletion. ADR assistant-dictionary/0001 D2, D4, D5.

The tag list and the withdrawn form are a cross-repo constant: Brainstorm-UI's
`exactCopyTemplate` and `withdrawnTemplate` (`client/src/lib/conceptCopy.ts`)
make the same tags for a customer's own copy, and both repos' tests pin them
(the way Trusted Lists' ADR 0001 D8 copies its concept `z` addresses).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from app.utils.client_tag import CLIENT_TAG

HEADER_KIND = 39998

B_DEFERRED = "b-tag-deferred"

WITHDRAWN_ALT = (
    "A withdrawn concept copy: considered, and affiliated with no community concept."
)

# The community header's definition, copied verbatim and in its order; nothing
# else is. Real community headers also carry `json`, `concept-graph`, `r`,
# `license`, `alt` and `client`, none of them the copier's to repeat.
# Brainstorm-UI `client/src/lib/conceptCopy.ts` `COPIED_TAGS`.
COPIED_TAGS = (
    "names",
    "name",
    "description",
    "slug",
    "required",
    "recommended",
    "optional",
    "allowed",
    "field-type",
    "display",
    "image",
    "link",
)

# `39998:<64 lowercase hex>:<d>`, split at the first two colons only: a `d` may
# contain colons, or anything else. Matched whole (`fullmatch`, `DOTALL`), so a
# coordinate always equals `39998:<pubkey>:<d>` as parsed: `re.match` with `$`
# would also take a trailing newline and drop it from the `d`, and the copy's
# `b` would then name a coordinate other than the one it copied.
_COORDINATE = re.compile(r"39998:([0-9a-f]{64}):(.+)", re.DOTALL)

AddOutcome = Literal["published", "updated", "unchanged", "conflict"]
WithdrawOutcome = Literal["withdrawn", "unchanged", "conflict"]


def parse_concept_coordinate(value: str) -> tuple[str, str] | None:
    """`(pubkey, d)` for a kind-39998 coordinate, else None. A string that
    isn't valid UTF-8 (a lone surrogate) is no coordinate: it couldn't be
    signed or answered."""
    match = _COORDINATE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return match.group(1), match.group(2)


def d_of(event: dict[str, Any]) -> str | None:
    """A header's identifier: the value of its first `d` tag, as nostr-sdk and
    Brainstorm-UI read it. None when it has no `d` tag, or its first one has no
    string value: a later `d` never stands in."""
    tags = event.get("tags")
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, list) and tag and tag[0] == "d":
            return tag[1] if len(tag) > 1 and isinstance(tag[1], str) else None
    return None


def copy_tags(community: dict[str, Any], coordinate: str) -> list[list[str]]:
    """The Assistant's copy of `community`: its `d`, its definition tags
    verbatim and in order, a pointer back, and the client tag."""
    tags = [t for t in community.get("tags") or [] if isinstance(t, list) and t]
    d = d_of(community) or ""
    definition = [list(t) for t in tags if t[0] in COPIED_TAGS]
    return [["d", d], *definition, ["b", coordinate, "pointer"], list(CLIENT_TAG)]


def withdrawn_tags(d: str) -> list[list[str]]:
    """The same coordinate, no longer a copy of anything."""
    return [["d", d], ["b", B_DEFERRED], ["alt", WITHDRAWN_ALT], list(CLIENT_TAG)]


def _b_tags(event: dict[str, Any]) -> list[list[str]]:
    return [
        t
        for t in event.get("tags") or []
        if isinstance(t, list) and len(t) > 1 and t[0] == "b"
    ]


def points_at(event: dict[str, Any], coordinate: str) -> bool:
    """A `b` of any type names the coordinate (Brainstorm-UI's `pointsAt`).
    The deferred `b` names nothing."""
    return any(t[1] == coordinate for t in _b_tags(event))


def is_withdrawn(event: dict[str, Any]) -> bool:
    """Its `b` is the deferred one and nothing else."""
    b_tags = _b_tags(event)
    return bool(b_tags) and all(t[1] == B_DEFERRED for t in b_tags)


def _without_client(tags: list[list[str]]) -> list[list[str]]:
    return [list(t) for t in tags if isinstance(t, list) and t and t[0] != "client"]


def add_outcome(
    composed: list[list[str]], newest: dict[str, Any] | None, coordinate: str
) -> AddOutcome:
    """What adding does, given the Assistant's newest header at the copy's `d`
    (D4). It never overwrites a header that isn't its copy of this concept."""
    if newest is None or is_withdrawn(newest):
        return "published"
    if points_at(newest, coordinate):
        held = _without_client(newest.get("tags") or [])
        return "unchanged" if held == _without_client(composed) else "updated"
    return "conflict"


def withdraw_outcome(newest: dict[str, Any] | None, coordinate: str) -> WithdrawOutcome:
    """What withdrawing does, given the Assistant's newest header at the `d` (D4)."""
    if newest is None or is_withdrawn(newest):
        return "unchanged"
    if points_at(newest, coordinate):
        return "withdrawn"
    return "conflict"


def next_created_at(now: int, newest: dict[str, Any] | None) -> int:
    """At least a second after the newest version, so a new one always replaces
    it, even one published this same second or dated ahead of us (D5)."""
    if newest is None:
        return now
    return max(now, int(newest.get("created_at") or 0) + 1)
