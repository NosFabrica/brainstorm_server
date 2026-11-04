"""Working out what was tagged: each event tagging's tagging target and member.

ADR `trusted-lists/0002` D2 (the rule) and D6 (the lookup). A coordinate
carries its kind, so most targets need no fetch. A DList item's DList, and the
kind of an event tagged by id, need the event itself, so they are looked up on
the configured relays (`trusted_list_target_relays`) plus the tagging's relay
hint.

**What is cached:** only lookups by event id, forever, because an id names one
immutable event (`event_tagging_repo`'s `nostr_tagged_event`). Anything named
by coordinate is fetched fresh every run: a replaceable item can move to
another DList, and a header can be renamed.

**A failed lookup** (any configured relay unreachable) raises `LookupFailed`,
and the caller treats the whole content view as untrustworthy (D8): it
publishes and retracts no content list. A hint relay failing is ignored.

The resolution itself (`resolve_target`, `dlist_name`) is pure, over the facts
the lookups return, so it is tested without a relay.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

from nostr_sdk import Client, EventId, Filter, Kind, PublicKey  # type: ignore

from app.core.config import settings
from app.core.loggr import loggr
from app.repos.event_tagging_repo import TaggedEvent
from app.services.tagging_parse import TAGGING_KIND, parse_coordinate

logger = loggr.get_logger(__name__)

LIST_KIND_EVENTS = 30393  # members by event id
LIST_KIND_ADDRESSES = 30394  # members by coordinate

DLIST_HEADER_KIND = 39998
DLIST_HEADER_ID_KIND = 9998  # the regular (non-replaceable) header kind
DLIST_ITEM_KIND = TAGGING_KIND  # 39999, replaceable, named by coordinate
DLIST_ITEM_ID_KIND = 9999  # regular, named by id

ID_CHUNK = 200
FETCH_TIMEOUT = timedelta(seconds=20)
CONNECT_TIMEOUT = timedelta(seconds=10)
SEARCH_TOKEN = "include:spam"


def is_addressable(kind: int) -> bool:
    return 30000 <= kind < 40000


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


# --- facts ------------------------------------------------------------------


def facts_of(event: dict) -> TaggedEvent:
    """The fields resolution needs, off a fetched event."""
    tags = [t for t in event.get("tags") or [] if isinstance(t, list) and t]
    d_tag = next(
        (t[1] for t in tags if t[0] == "d" and len(t) >= 2 and isinstance(t[1], str)),
        None,
    )
    names = next(
        (
            tuple(v for v in t[1:] if isinstance(v, str))
            for t in tags
            if t[0] == "names"
        ),
        (),
    )
    return TaggedEvent(
        event_id=str(event.get("id")),
        kind=int(event.get("kind") or 0),
        pubkey=str(event.get("pubkey")),
        d_tag=d_tag,
        z_values=tuple(
            t[1] for t in tags if t[0] == "z" and len(t) >= 2 and isinstance(t[1], str)
        ),
        names=names,
    )


def coordinate_of(facts: TaggedEvent) -> str:
    return f"{facts.kind}:{facts.pubkey}:{facts.d_tag or ''}"


def header_refs(facts: TaggedEvent) -> list[str]:
    """The DList headers a DList item names in its `z` tags: a `39998`
    coordinate, or a kind-9998 header's event id. Order kept, duplicates
    dropped. Any other `z` (a concept handle of another kind) isn't a header."""
    refs: list[str] = []
    for value in facts.z_values:
        coordinate = parse_coordinate(value)
        is_header = (
            coordinate is not None and coordinate.kind == DLIST_HEADER_KIND
        ) or _is_hex64(value)
        if is_header and value not in refs:
            refs.append(value)
    return refs


# --- resolution (pure) -------------------------------------------------------


@dataclass(frozen=True)
class TaggingTarget:
    """What a list is a list *of*: an event kind, or one DList (by its header's
    coordinate or, for a kind-9998 header, its event id)."""

    kind: int | None = None
    dlist: str | None = None

    @property
    def value(self) -> str:
        """The `tagging-target` tag's value (D5)."""
        return str(self.kind) if self.dlist is None else self.dlist


@dataclass(frozen=True)
class Resolution:
    list_kind: int  # 30393 or 30394
    member: str  # an event id (30393) or a coordinate (30394)
    targets: tuple[TaggingTarget, ...]


def _item_targets(facts: TaggedEvent | None, kind: int) -> tuple[TaggingTarget, ...]:
    refs = header_refs(facts) if facts is not None else []
    if refs:
        # An item claiming two DLists is a member of both lists (D2).
        return tuple(TaggingTarget(dlist=ref) for ref in refs)
    return (TaggingTarget(kind=kind),)


def resolve_target(
    target: str,
    slot: str,
    by_id: dict[str, TaggedEvent],
    by_coordinate: dict[str, TaggedEvent],
) -> Resolution | None:
    """ADR 0002 D2's table, or None when the facts it needs weren't found.

    `by_id` holds what lookups by id returned (cached or fresh); `by_coordinate`
    the 39999 items fetched by coordinate this run.
    """
    if slot == "a":
        coordinate = parse_coordinate(target)
        if coordinate is None:
            return None
        return _resolve_coordinate(
            str(coordinate), coordinate.kind, by_coordinate, None
        )

    facts = by_id.get(target)
    if facts is None:
        return None
    if is_addressable(facts.kind):
        # Tagged by id though addressable (legacy, Brainstorm-UI#334): the member
        # is its coordinate, so it merges with taggings by coordinate (D2, D7).
        return _resolve_coordinate(
            coordinate_of(facts), facts.kind, by_coordinate, facts
        )
    if facts.kind == DLIST_ITEM_ID_KIND:
        return Resolution(LIST_KIND_EVENTS, target, _item_targets(facts, facts.kind))
    return Resolution(LIST_KIND_EVENTS, target, (TaggingTarget(kind=facts.kind),))


def _resolve_coordinate(
    coordinate: str,
    kind: int,
    by_coordinate: dict[str, TaggedEvent],
    fallback: TaggedEvent | None,
) -> Resolution | None:
    if kind != DLIST_ITEM_KIND:
        return Resolution(LIST_KIND_ADDRESSES, coordinate, (TaggingTarget(kind=kind),))
    # A DList item's list comes from its newest version, fetched this run. An
    # item seen only through a lookup by id (an older version) falls back to
    # that version's headers; one seen nowhere is unresolved.
    facts = by_coordinate.get(coordinate) or fallback
    if facts is None:
        return None
    return Resolution(LIST_KIND_ADDRESSES, coordinate, _item_targets(facts, kind))


def dlist_name(
    ref: str,
    headers_by_coordinate: dict[str, TaggedEvent],
    by_id: dict[str, TaggedEvent],
) -> str | None:
    """A DList's name for titles (D4): its header's plural from `names`, else
    the singular, else the header's `d`. A kind-9998 header named by id has no
    `d`, so one that wasn't found, or has no names, can't be named (None)."""
    coordinate = parse_coordinate(ref)
    facts = headers_by_coordinate.get(ref) if coordinate is not None else by_id.get(ref)
    if facts is not None:
        names = [n for n in facts.names if n.strip()]
        if len(names) >= 2:
            return names[1]
        if names:
            return names[0]
    if coordinate is not None:
        return coordinate.d_tag or None
    return None


# --- lookups -----------------------------------------------------------------


class LookupFailed(Exception):
    """A configured lookup relay couldn't be read. The run's content view is
    then untrustworthy (ADR 0002 D8)."""


def _is_search_relay(url: str) -> bool:
    """A relay on `search_relay_url`'s host serves anonymous reads only with the
    `include:spam` search token; any other relay would reject the key."""
    search_host = urlparse(settings.search_relay_url).hostname
    return bool(search_host) and urlparse(url).hostname == search_host


async def _fetch_from_relay(url: str, filters: list[Filter]) -> list[dict]:
    """Every event `url` returns for `filters`. Raises when it can't be reached.

    The network boundary: tests patch this, so nothing else here needs a relay.
    """
    client = Client()
    try:
        await client.add_relay(url)
        output = await client.try_connect(CONNECT_TIMEOUT)
        if output.failed or not output.success:
            raise ConnectionError(f"could not connect to {url}")
        if _is_search_relay(url):
            filters = [f.search(SEARCH_TOKEN) for f in filters]
        events: list[dict] = []
        for flt in filters:
            fetched = await client.fetch_events(flt, timeout=FETCH_TIMEOUT)
            for event in fetched.to_vec():
                if event.verify():
                    events.append(json.loads(event.as_json()))
        return events
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — teardown only
            pass


@dataclass
class Lookup:
    """One run's lookups. Configured relays must all answer; hints are extras."""

    relays: list[str] = field(
        default_factory=lambda: list(settings.trusted_list_target_relays)
    )

    async def _gather(self, filters: list[Filter], hints: Iterable[str]) -> list[dict]:
        events: list[dict] = []
        if not filters:
            return events
        for url in self.relays:
            try:
                events.extend(await _fetch_from_relay(url, filters))
            except Exception as exc:  # noqa: BLE001
                raise LookupFailed(f"{url}: {exc}") from exc
        for url in sorted(set(hints) - set(self.relays)):
            try:
                events.extend(await _fetch_from_relay(url, filters))
            except Exception as exc:  # noqa: BLE001 — best effort
                logger.warning("Lookup hint relay %s failed: %s", url, exc)
        return events

    async def by_ids(
        self, ids: Iterable[str], hints: Iterable[str] = ()
    ) -> dict[str, TaggedEvent]:
        """The events with these ids. An answer whose id wasn't asked for is
        dropped, so a relay can't substitute one event for another."""
        wanted = sorted(set(ids))
        filters = [
            Filter().ids([EventId.parse(i) for i in wanted[n : n + ID_CHUNK]])
            for n in range(0, len(wanted), ID_CHUNK)
        ]
        found: dict[str, TaggedEvent] = {}
        for event in await self._gather(filters, hints):
            if event.get("id") in wanted:
                found[event["id"]] = facts_of(event)
        return found

    async def by_coordinates(
        self, coordinates: Iterable[str], hints: Iterable[str] = ()
    ) -> dict[str, TaggedEvent]:
        """The newest event at each coordinate. An answer at another coordinate
        is dropped."""
        groups: dict[tuple[int, str], set[str]] = {}
        wanted: set[str] = set()
        for value in coordinates:
            coordinate = parse_coordinate(value)
            if coordinate is None:
                continue
            wanted.add(str(coordinate))
            groups.setdefault((coordinate.kind, coordinate.pubkey), set()).add(
                coordinate.d_tag
            )
        filters = [
            Filter()
            .kinds([Kind(kind)])
            .authors([PublicKey.parse(pubkey)])
            .identifiers(sorted(d_tags))
            for (kind, pubkey), d_tags in sorted(groups.items())
        ]
        newest: dict[str, tuple[int, TaggedEvent]] = {}
        for event in await self._gather(filters, hints):
            facts = facts_of(event)
            key = coordinate_of(facts)
            created_at = int(event.get("created_at") or 0)
            if key in wanted and (key not in newest or newest[key][0] < created_at):
                newest[key] = (created_at, facts)
        return {key: facts for key, (_, facts) in newest.items()}
