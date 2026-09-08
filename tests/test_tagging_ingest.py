"""kind-39999 parsing: tag elements, taggings, and what must be ignored.

Pure-function tests over event dicts, mirroring tests/test_kind0_ingest.py's
pattern of calling the extractor directly.
"""
from __future__ import annotations

import json

import pytest

from app.nostr_event_transferer.nostr_event_transferer import ev_kinds
from app.services.tagging_parse import (
    NOSTR_USER_TAG_Z_TAG,
    TAG_Z_TAG,
    TAGGING_KIND,
    parse_tag_element,
    parse_user_tagging,
    read_polarity,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64


def _element(tags=None, content=None, event_id=A, pubkey=B, created_at=100):
    payload = {"tag": {"slug": "podcaster", "name": "Podcaster", "description": "Makes podcasts"}}
    return {
        "id": event_id,
        "pubkey": pubkey,
        "kind": TAGGING_KIND,
        "created_at": created_at,
        "content": json.dumps(payload) if content is None else content,
        "tags": [["z", TAG_Z_TAG], ["d", "podcaster"]] if tags is None else tags,
    }


def _tagging(tags=None, event_id=C, pubkey=D, created_at=200):
    return {
        "id": event_id,
        "pubkey": pubkey,
        "kind": TAGGING_KIND,
        "created_at": created_at,
        "content": "",
        "tags": [
            ["z", NOSTR_USER_TAG_Z_TAG],
            ["d", "profile-tag-podcaster-aaaaaaaa-bbbbbbbb"],
            ["p", A],
            ["e", A],
            ["polarity", "1"],
        ] if tags is None else tags,
    }


# --- AC1 -------------------------------------------------------------------

def test_tag_element_parsed_from_event():
    el = parse_tag_element(_element())
    assert el is not None
    assert el.slug == "podcaster"
    assert el.name == "Podcaster"
    assert el.description == "Makes podcasts"
    assert el.author_pubkey == B
    assert el.created_at_unix == 100


def test_tag_element_name_falls_back_to_slug():
    ev = _element(content=json.dumps({"tag": {"slug": "chef"}}))
    el = parse_tag_element(ev)
    assert el is not None
    # A TL's title comes from here; an empty title would be a useless list.
    assert el.name == "chef"
    assert el.description == ""


# --- AC2 -------------------------------------------------------------------

def test_tagging_parsed_from_event():
    tg = parse_user_tagging(_tagging())
    assert tg is not None
    assert tg.asserter_pubkey == D
    assert tg.target_pubkey == A
    assert tg.tag_event_id == A
    assert tg.polarity == 1.0
    assert tg.d_tag == "profile-tag-podcaster-aaaaaaaa-bbbbbbbb"


def test_tagging_without_d_tag_gets_deterministic_fallback():
    # No `d` means no replaceability key on the wire; we synthesize the natural
    # identity so republishes collapse instead of accumulating a row each time.
    ev = _tagging(tags=[["z", NOSTR_USER_TAG_Z_TAG], ["p", A], ["e", B]])
    tg = parse_user_tagging(ev)
    assert tg is not None
    assert tg.d_tag == f"{A}:{B}"


# --- AC3 -------------------------------------------------------------------

def test_unknown_z_tag_is_not_persisted():
    ev = _element(tags=[["z", "39998:deadbeef:something-else"]])
    assert parse_tag_element(ev) is None
    assert parse_user_tagging(ev) is None


def test_missing_z_tag_is_not_persisted():
    assert parse_tag_element(_element(tags=[["d", "x"]])) is None
    assert parse_user_tagging(_tagging(tags=[["p", A], ["e", B]])) is None


# --- AC4 -------------------------------------------------------------------

@pytest.mark.parametrize(
    "tags",
    [
        [["z", NOSTR_USER_TAG_Z_TAG], ["e", A]],                  # no p
        [["z", NOSTR_USER_TAG_Z_TAG], ["p", A]],                  # no e
        [["z", NOSTR_USER_TAG_Z_TAG], ["p", "short"], ["e", A]],  # bad p
        [["z", NOSTR_USER_TAG_Z_TAG], ["p", A], ["e", "nope"]],   # bad e
    ],
)
def test_malformed_tagging_returns_none(tags):
    assert parse_user_tagging(_tagging(tags=tags)) is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"tag": {}}),               # no slug
        json.dumps({"tag": {"slug": "   "}}),  # blank slug
        json.dumps({"notatag": 1}),
        "",
    ],
)
def test_malformed_tag_element_returns_none(content):
    assert parse_tag_element(_element(content=content)) is None


def test_non_hex_ids_rejected():
    assert parse_tag_element(_element(event_id="xyz")) is None
    assert parse_user_tagging(_tagging(pubkey="xyz")) is None


# --- AC7 -------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("1", 1.0), ("-1", -1.0), ("0.5", 0.5), ("-0.5", -0.5), ("0", 0.0)],
)
def test_read_polarity_values(raw, expected):
    assert read_polarity(_tagging(tags=[["polarity", raw]])) == expected


def test_absent_polarity_defaults_to_applied():
    assert read_polarity({"tags": []}) == 1.0


def test_unparseable_polarity_defaults_to_applied():
    assert read_polarity({"tags": [["polarity", "banana"]]}) == 1.0


# --- S1: regression sentinel ----------------------------------------------

def test_ev_kinds_still_only_graph_kinds():
    """`ev_kinds` must NOT gain kind 39999 — ADR trusted-lists/0001 D10.

    `backfill_redis_relationships._is_graph_db_populated` iterates this list and
    gates the one-time Redis relationship backfill on EVERY listed kind having a
    completed transfer row. Adding a kind here silently disables that backfill
    until the new kind's transfer finishes. Taggings sync via
    `tagging_ev_kinds` precisely so this list keeps its meaning.

    This sentinel passes before the feature exists, by design.
    """
    kinds = {k.as_u16() for k, _ in ev_kinds}
    assert kinds == {0, 3, 5, 1984, 10000}
    assert TAGGING_KIND not in kinds
