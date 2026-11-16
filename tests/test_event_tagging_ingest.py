"""Event taggings and tagging headers at ingest (story 2, AC1-AC5).

Pure parsers over event dicts, plus the 39999 dispatch with its writers
patched. ADR `trusted-lists/0002` D6. The shapes are the ones on the tag hub
(checked 2026-11-03): a header's concept `z` and `a` to its Tag element; an
event tagging's concept `z`, its descriptor `z` to the header's coordinate,
and one target.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.message_queue_tasks import process_strfry_event as ingest
from app.services.tagging_parse import (
    LEGACY_Z_TAG_PUBKEY,
    NOSTR_USER_TAG_Z_TAG,
    TAG_Z_TAG,
    parse_event_tagging,
    parse_tagging_header,
)

LEGACY = LEGACY_Z_TAG_PUBKEY
CLIENT = "a68dbf561cfe3da1b76f1e65c7d4d9cc116f79921b38a815fd75cb5460b4b599"
AUTHOR = "2efaa715bbb46dd5be6b7da8d7700266d11674b913b8178addb5c2e63d987331"
ASSERTER = "b" * 64
NOTE_ID = "1de401a06873489a708b37f6442b2b302d7a2e04eafaa8991a33a0598c1f737f"
ARTICLE = "30023:" + "c" * 64 + ":https://example.org/a:b"
HEADER_D = "tagging:espresso-tagging"
HEADER_COORD = f"39999:{AUTHOR}:{HEADER_D}"


def _header(namespaces=(LEGACY, CLIENT), *, tag_ref=("a", f"39999:{AUTHOR}:espresso"), **over):
    tags = [["d", HEADER_D], ["names", "Tagging of an event as espresso"]]
    tags += [["z", f"39998:{ns}:tagging-with-specific-tag"] for ns in namespaces]
    if tag_ref is not None:
        tags.append(list(tag_ref))
    event = {
        "kind": 39999,
        "id": "e" * 64,
        "pubkey": AUTHOR,
        "created_at": 1793296150,
        "tags": tags,
        "content": "",
    }
    event.update(over)
    return event


def _tagging(
    namespaces=(LEGACY, CLIENT),
    *,
    target=("e", NOTE_ID),
    descriptor=HEADER_COORD,
    polarity="1",
    extra=(),
    **over,
):
    tags = [["d", "event-tag-espresso-1de401a0-2efaa715"]]
    if target is not None:
        tags.append(list(target))
    tags += [["z", f"39998:{ns}:nostr-event-tag"] for ns in namespaces]
    if descriptor is not None:
        tags.append(["z", descriptor])
    tags.append(["polarity", polarity])
    tags += [list(t) for t in extra]
    event = {
        "kind": 39999,
        "id": "f" * 64,
        "pubkey": ASSERTER,
        "created_at": 1793296150,
        "tags": tags,
        "content": "",
    }
    event.update(over)
    return event


# --- U1: tagging header (AC1) ----------------------------------------------


@pytest.mark.parametrize(
    "tag_ref, expected",
    [
        (("a", f"39999:{AUTHOR}:espresso"), (AUTHOR, "espresso", None)),
        (("e", "d" * 64), (None, None, "d" * 64)),
    ],
    ids=["a", "e"],
)
def test_tagging_header_parsed_from_event(tag_ref, expected):
    header = parse_tagging_header(_header(tag_ref=tag_ref))
    assert header is not None
    assert (header.tag_author_pubkey, header.tag_slug, header.tag_event_id) == expected
    assert header.author_pubkey == AUTHOR
    assert header.d_tag == HEADER_D
    assert header.coordinate == HEADER_COORD
    assert header.created_at_unix == 1793296150


# --- U2: event tagging (AC2) -----------------------------------------------


@pytest.mark.parametrize(
    "target, slot, value",
    [
        (("e", NOTE_ID, "wss://relay.example"), "e", NOTE_ID),
        (("a", ARTICLE), "a", ARTICLE),
    ],
    ids=["e", "a"],
)
def test_event_tagging_parsed_from_event(target, slot, value):
    tagging = parse_event_tagging(_tagging(target=target, polarity="-1"))
    assert tagging is not None
    assert tagging.event_id == "f" * 64
    assert tagging.asserter_pubkey == ASSERTER
    assert tagging.d_tag == "event-tag-espresso-1de401a0-2efaa715"
    assert (tagging.target, tagging.target_slot) == (value, slot)
    assert tagging.target_relay == ("wss://relay.example" if slot == "e" else None)
    assert tagging.header_coordinate == HEADER_COORD
    assert tagging.polarity == -1.0
    assert tagging.created_at_unix == 1793296150


def test_article_coordinate_with_colons_in_its_d_is_kept_whole():
    tagging = parse_event_tagging(_tagging(target=("a", ARTICLE)))
    assert tagging is not None and tagging.target == ARTICLE


# --- U3: honored namespaces (AC3) ------------------------------------------


@pytest.mark.parametrize(
    "namespaces, ingested",
    [((LEGACY,), True), ((CLIENT,), True), ((LEGACY, CLIENT), True), (("9" * 64,), False)],
    ids=["legacy", "client", "both", "neither"],
)
def test_honored_namespaces(namespaces, ingested):
    assert (parse_tagging_header(_header(namespaces)) is not None) is ingested
    assert (parse_event_tagging(_tagging(namespaces)) is not None) is ingested


def test_honored_set_comes_from_the_setting(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "tagging_honored_namespaces", [LEGACY])
    assert parse_event_tagging(_tagging((CLIENT,))) is None
    assert parse_tagging_header(_header((CLIENT,))) is None
    assert parse_event_tagging(_tagging((LEGACY,))) is not None


# --- U4: malformed and foreign (AC4) ---------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        _tagging(target=None),
        _tagging(extra=[("a", ARTICLE)]),
        _tagging(descriptor=None),
        _tagging(descriptor=f"39998:{AUTHOR}:tagging:espresso-tagging"),
        _tagging(target=("e", "not-hex")),
        _tagging(target=("a", "1:" + "c" * 64 + ":d")),
        _tagging(target=("a", "30023:short:d")),
        _tagging(kind=1),
        _tagging(id="nope"),
    ],
    ids=[
        "no target",
        "both a and e",
        "no descriptor z",
        "descriptor not a header coordinate",
        "target id not 64-hex",
        "target coordinate of a non-addressable kind",
        "target coordinate malformed",
        "not kind 39999",
        "bad event id",
    ],
)
def test_malformed_event_tagging_returns_none(event):
    assert parse_event_tagging(event) is None


@pytest.mark.parametrize(
    "event",
    [
        _header(tag_ref=None),
        _header(tag_ref=("a", f"30023:{AUTHOR}:espresso")),
        _header(tag_ref=("e", "short")),
        _header(tags=[["z", f"39998:{LEGACY}:tagging-with-specific-tag"]]),
    ],
    ids=["no tag ref", "a not a tag element", "e not hex", "no d"],
)
def test_malformed_tagging_header_returns_none(event):
    assert parse_tagging_header(event) is None


def _patch_writers(monkeypatch):
    writes: list[tuple[str, object]] = []

    @asynccontextmanager
    async def fake_session():
        yield MagicMock()

    def writer(name):
        async def write(_db, value):
            writes.append((name, value))

        return write

    monkeypatch.setattr(ingest, "db_session", fake_session)
    for name in (
        "upsert_tag_element_on_db",
        "upsert_user_tagging_on_db",
        "upsert_tagging_header_on_db",
        "upsert_event_tagging_on_db",
    ):
        monkeypatch.setattr(ingest, name, writer(name))
    return writes


def test_malformed_event_tagging_does_not_stop_the_batch(monkeypatch):
    writes = _patch_writers(monkeypatch)
    batch = [_tagging(), _tagging(target=None), _tagging(id="9" * 64)]

    async def run():
        for event in batch:
            await ingest.process_event_kind_39999(event)

    asyncio.run(run())
    assert [name for name, _ in writes] == ["upsert_event_tagging_on_db"] * 2


# --- U5: story 1's shapes still go to story 1's writers (AC5) ----------------


def test_pubkey_tagging_and_tag_element_still_route_to_story_1_writers(monkeypatch):
    writes = _patch_writers(monkeypatch)
    element = {
        "kind": 39999,
        "id": "1" * 64,
        "pubkey": AUTHOR,
        "created_at": 1,
        "tags": [["d", "espresso"], ["z", TAG_Z_TAG]],
        "content": '{"tag": {"slug": "espresso", "name": "espresso"}}',
    }
    pubkey_tagging = {
        "kind": 39999,
        "id": "2" * 64,
        "pubkey": ASSERTER,
        "created_at": 1,
        "tags": [
            ["d", "x"],
            ["z", NOSTR_USER_TAG_Z_TAG],
            ["p", "3" * 64],
            ["e", "1" * 64],
        ],
        "content": "",
    }

    async def run():
        for event in (element, pubkey_tagging, _header(), _tagging()):
            await ingest.process_event_kind_39999(event)

    asyncio.run(run())
    assert [name for name, _ in writes] == [
        "upsert_tag_element_on_db",
        "upsert_user_tagging_on_db",
        "upsert_tagging_header_on_db",
        "upsert_event_tagging_on_db",
    ]


def test_foreign_39999_writes_nothing(monkeypatch):
    writes = _patch_writers(monkeypatch)
    foreign = _tagging(namespaces=())
    asyncio.run(ingest.process_event_kind_39999(foreign))
    assert writes == []
