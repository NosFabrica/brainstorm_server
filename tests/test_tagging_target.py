"""Working out a tagging target (story 2, AC6, AC7; ADR `trusted-lists/0002` D2, D6).

The resolution is pure over the facts lookups return. The lookups are tested
with their one network boundary, `_fetch_from_relay`, patched by a fake relay
that answers every filter with everything it holds: `Lookup` must itself drop
whatever wasn't asked for.
"""
from __future__ import annotations

import asyncio

import pytest
from nostr_sdk import Keys

from app.repos.event_tagging_repo import TaggedEvent
from app.services import tagging_target_service as tts
from app.services.tagging_target_service import (
    Lookup,
    LookupFailed,
    Resolution,
    TaggingTarget,
    dlist_name,
    resolve_target,
)

AUTHOR = Keys.generate().public_key().to_hex()
CURATOR = Keys.generate().public_key().to_hex()
NS = "82b75e474dda005e912bcbb910391c60c2b89cc7faf5d3c30b7c59a324973833"
BOOKS = f"39998:{CURATOR}:books"
SHOPS = f"39998:{CURATOR}:shops"
HEADER_9998 = "9" * 64


def facts(kind, *, id="0" * 64, pubkey=AUTHOR, d=None, z=(), names=()):
    return TaggedEvent(
        event_id=id,
        kind=kind,
        pubkey=pubkey,
        d_tag=d,
        z_values=tuple(z),
        names=tuple(names),
    )


ITEM = f"39999:{AUTHOR}:dune"


# --- U6: every row of D2's table --------------------------------------------


@pytest.mark.parametrize(
    "target, slot, by_id, by_coordinate, expected",
    [
        pytest.param(
            ITEM, "a", {}, {ITEM: facts(39999, d="dune", z=[BOOKS])},
            Resolution(30394, ITEM, (TaggingTarget(dlist=BOOKS),)),
            id="39999 by a, 39998 header",
        ),
        pytest.param(
            ITEM, "a", {}, {ITEM: facts(39999, d="dune", z=[HEADER_9998])},
            Resolution(30394, ITEM, (TaggingTarget(dlist=HEADER_9998),)),
            id="39999 by a, 9998 header id",
        ),
        pytest.param(
            "1" * 64, "e", {"1" * 64: facts(9999, id="1" * 64, z=[BOOKS])}, {},
            Resolution(30393, "1" * 64, (TaggingTarget(dlist=BOOKS),)),
            id="9999 by e",
        ),
        pytest.param(
            ITEM, "a", {}, {ITEM: facts(39999, d="dune")},
            Resolution(30394, ITEM, (TaggingTarget(kind=39999),)),
            id="headerless 39999",
        ),
        pytest.param(
            "1" * 64, "e", {"1" * 64: facts(9999, id="1" * 64)}, {},
            Resolution(30393, "1" * 64, (TaggingTarget(kind=9999),)),
            id="headerless 9999",
        ),
        pytest.param(
            f"30402:{AUTHOR}:bike", "a", {}, {},
            Resolution(30394, f"30402:{AUTHOR}:bike", (TaggingTarget(kind=30402),)),
            id="30402 by a, no fetch",
        ),
        pytest.param(
            "2" * 64, "e", {"2" * 64: facts(1, id="2" * 64)}, {},
            Resolution(30393, "2" * 64, (TaggingTarget(kind=1),)),
            id="note by e",
        ),
        pytest.param(
            "3" * 64, "e", {"3" * 64: facts(30023, id="3" * 64, d="essay")}, {},
            Resolution(30394, f"30023:{AUTHOR}:essay", (TaggingTarget(kind=30023),)),
            id="addressable by e -> its coordinate",
        ),
        pytest.param(
            f"39999:{AUTHOR}:espresso", "a", {},
            {f"39999:{AUTHOR}:espresso": facts(39999, d="espresso", z=[f"39998:{NS}:tag"])},
            Resolution(
                30394, f"39999:{AUTHOR}:espresso", (TaggingTarget(dlist=f"39998:{NS}:tag"),)
            ),
            id="a tagged Tag -> its concept header",
        ),
    ],
)
def test_tagging_target_of(target, slot, by_id, by_coordinate, expected):
    assert resolve_target(target, slot, by_id, by_coordinate) == expected


def test_id_tagged_addressable_member_is_its_coordinate():
    """Legacy (Brainstorm-UI#334): an item tagged by id is a member by its
    coordinate, resolved through its newest version's headers."""
    old = facts(39999, id="4" * 64, d="dune", z=[SHOPS])
    newest = facts(39999, id="5" * 64, d="dune", z=[BOOKS])
    resolution = resolve_target("4" * 64, "e", {"4" * 64: old}, {ITEM: newest})
    assert resolution == Resolution(30394, ITEM, (TaggingTarget(dlist=BOOKS),))
    # Without the newest version, the old version's headers still place it.
    fallback = resolve_target("4" * 64, "e", {"4" * 64: old}, {})
    assert fallback == Resolution(30394, ITEM, (TaggingTarget(dlist=SHOPS),))


def test_two_header_item_joins_both_lists():
    resolution = resolve_target(
        ITEM, "a", {}, {ITEM: facts(39999, d="dune", z=[BOOKS, SHOPS, BOOKS])}
    )
    assert resolution is not None
    assert resolution.targets == (TaggingTarget(dlist=BOOKS), TaggingTarget(dlist=SHOPS))


def test_item_moved_to_another_dlist_follows_it_next_run():
    """Coordinates are never cached (ADR 0002 D6): run 2 sees the new header."""
    run1 = resolve_target(ITEM, "a", {}, {ITEM: facts(39999, d="dune", z=[SHOPS])})
    run2 = resolve_target(ITEM, "a", {}, {ITEM: facts(39999, d="dune", z=[BOOKS])})
    assert run1 is not None and run1.targets == (TaggingTarget(dlist=SHOPS),)
    assert run2 is not None and run2.targets == (TaggingTarget(dlist=BOOKS),)


def test_unfound_targets_resolve_to_none():
    assert resolve_target("6" * 64, "e", {}, {}) is None
    assert resolve_target(ITEM, "a", {}, {}) is None


@pytest.mark.parametrize(
    "names, expected",
    [
        (("Book", "Books"), "Books"),
        (("Book",), "Book"),
        ((), "books"),
    ],
    ids=["plural", "singular-only", "d-only"],
)
def test_dlist_name(names, expected):
    headers = {BOOKS: facts(39998, pubkey=CURATOR, d="books", names=names)}
    assert dlist_name(BOOKS, headers, {}) == expected


def test_dlist_name_of_an_unfetched_header_falls_back_to_its_d():
    assert dlist_name(BOOKS, {}, {}) == "books"


def test_dlist_name_of_a_9998_header_needs_the_header():
    assert dlist_name(HEADER_9998, {}, {}) is None
    by_id = {HEADER_9998: facts(9998, id=HEADER_9998, names=("Pod", "Pods"))}
    assert dlist_name(HEADER_9998, {}, by_id) == "Pods"


# --- lookups --------------------------------------------------------------


def _event(kind, *, id, pubkey=AUTHOR, d=None, z=(), created_at=1):
    tags = []
    if d is not None:
        tags.append(["d", d])
    tags += [["z", v] for v in z]
    return {"id": id, "kind": kind, "pubkey": pubkey, "created_at": created_at, "tags": tags}


def _fake_relays(monkeypatch, stores, down=()):
    """`stores` maps relay URL -> events it answers with; `down` can't be reached."""
    calls: list[str] = []

    async def fetch(url, filters):
        calls.append(url)
        if url in down:
            raise ConnectionError("unreachable")
        return list(stores.get(url, []))

    monkeypatch.setattr(tts, "_fetch_from_relay", fetch)
    return calls


def test_unfound_target_is_counted_and_looked_up_next_run(monkeypatch):
    """AC7. What isn't found isn't cached, so the next run asks again."""
    calls = _fake_relays(monkeypatch, {"wss://a": []})
    lookup = Lookup(relays=["wss://a"])
    assert asyncio.run(lookup.by_ids(["7" * 64])) == {}
    _fake_relays(monkeypatch, {"wss://a": [_event(1, id="7" * 64)]})
    assert set(asyncio.run(lookup.by_ids(["7" * 64]))) == {"7" * 64}
    assert calls == ["wss://a"]


def test_lookup_answer_not_matching_the_request_is_ignored(monkeypatch):
    _fake_relays(
        monkeypatch,
        {
            "wss://a": [
                _event(1, id="8" * 64),  # not the id asked for
                _event(39999, id="9" * 64, d="other", z=[SHOPS]),  # another d
                _event(39999, id="a" * 64, pubkey=CURATOR, d="dune", z=[SHOPS]),
            ]
        },
    )
    lookup = Lookup(relays=["wss://a"])
    assert asyncio.run(lookup.by_ids(["7" * 64])) == {}
    assert asyncio.run(lookup.by_coordinates([ITEM])) == {}


def test_coordinate_lookup_keeps_the_newest_version(monkeypatch):
    _fake_relays(
        monkeypatch,
        {
            "wss://a": [_event(39999, id="1" * 64, d="dune", z=[SHOPS], created_at=1)],
            "wss://b": [_event(39999, id="2" * 64, d="dune", z=[BOOKS], created_at=2)],
        },
    )
    found = asyncio.run(Lookup(relays=["wss://a", "wss://b"]).by_coordinates([ITEM]))
    assert found[ITEM].z_values == (BOOKS,)


def test_a_configured_relay_down_fails_the_lookup(monkeypatch):
    _fake_relays(monkeypatch, {"wss://hint": [_event(1, id="7" * 64)]}, down={"wss://a"})
    with pytest.raises(LookupFailed):
        asyncio.run(Lookup(relays=["wss://a"]).by_ids(["7" * 64], hints=["wss://hint"]))


def test_a_hint_relay_down_is_ignored(monkeypatch):
    _fake_relays(monkeypatch, {"wss://a": [_event(1, id="7" * 64)]}, down={"wss://hint"})
    found = asyncio.run(Lookup(relays=["wss://a"]).by_ids(["7" * 64], hints=["wss://hint"]))
    assert set(found) == {"7" * 64}


def test_nothing_to_look_up_contacts_no_relay(monkeypatch):
    calls = _fake_relays(monkeypatch, {}, down={"wss://a"})
    lookup = Lookup(relays=["wss://a"])
    assert asyncio.run(lookup.by_ids([])) == {}
    assert asyncio.run(lookup.by_coordinates([])) == {}
    assert calls == []


def test_search_relay_lookup_carries_include_spam(monkeypatch):
    """The search relay refuses anonymous reads without the token; any other
    relay would reject the `search` key, so it gets none."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "search_relay_url", "https://search.example")
    assert tts._is_search_relay("wss://search.example")
    assert tts._is_search_relay("wss://search.example/")
    assert not tts._is_search_relay("wss://dcosl.example")

    seen: list[str] = []

    class FakeEvents:
        def to_vec(self):
            return []

    class FakeClient:
        async def add_relay(self, url):
            pass

        async def try_connect(self, timeout):
            class Out:
                success = {"wss://search.example"}
                failed: dict = {}

            return Out()

        async def fetch_events(self, flt, timeout):
            seen.append(flt.as_json())
            return FakeEvents()

        async def disconnect(self):
            pass

    monkeypatch.setattr(tts, "Client", FakeClient)
    from nostr_sdk import Filter, Kind

    asyncio.run(tts._fetch_from_relay("wss://search.example", [Filter().kinds([Kind(1)])]))
    asyncio.run(tts._fetch_from_relay("wss://dcosl.example", [Filter().kinds([Kind(1)])]))
    assert '"search":"include:spam"' in seen[0]
    assert "search" not in seen[1]
