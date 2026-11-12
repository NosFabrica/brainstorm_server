"""`fetch_profiles`: kind-0s by author from our search relay, read unranked."""

import asyncio

from app.core import search_relay

ALICE = "a" * 64
BOB = "b" * 64


def _run(monkeypatch, events):
    asked = []

    async def fake_fetch(nostr_filter):
        asked.append(nostr_filter)
        return events

    monkeypatch.setattr(search_relay, "fetch_events", fake_fetch)
    return asyncio.run(search_relay.fetch_profiles([ALICE, BOB])), asked


def test_reads_kind0_by_author_unranked(monkeypatch):
    profile = {"kind": 0, "pubkey": ALICE, "created_at": 1, "content": "{}"}
    profiles, asked = _run(monkeypatch, [profile])

    assert profiles == {ALICE: profile}
    assert asked == [
        {"kinds": [0], "authors": [ALICE, BOB], "limit": 2, "search": "include:spam"}
    ]


def test_keeps_each_authors_newest_and_drops_other_kinds(monkeypatch):
    older = {"kind": 0, "pubkey": ALICE, "created_at": 1}
    newer = {"kind": 0, "pubkey": ALICE, "created_at": 2}
    note = {"kind": 1, "pubkey": BOB, "created_at": 3}
    profiles, _ = _run(monkeypatch, [older, newer, note])

    assert profiles == {ALICE: newer}


def test_no_pubkeys_asks_nothing(monkeypatch):
    async def fail(nostr_filter):
        raise AssertionError("should not read")

    monkeypatch.setattr(search_relay, "fetch_events", fail)
    assert asyncio.run(search_relay.fetch_profiles([])) == {}


def test_a_line_separator_inside_a_profile_does_not_split_its_line():
    """Regression: `splitlines()` broke NDJSON at U+2028, which JSON allows
    unescaped in a string — and real profiles carry it — so one such profile
    failed the whole read as "a line that is not JSON"."""
    import json
    from unittest.mock import patch

    import httpx

    profile = {
        "kind": 0,
        "pubkey": ALICE,
        "created_at": 1,
        "content": json.dumps({"about": "line one line two"}, ensure_ascii=False),
    }
    body = "\n".join(
        json.dumps(frame, ensure_ascii=False)
        for frame in (["EVENT", "ore", profile], ["EOSE", "ore"])
    )
    assert " " in body
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
        base_url="http://relay.test",
    )
    with patch("app.core.search_relay._client", client):
        profiles = asyncio.run(search_relay.fetch_profiles([ALICE]))

    assert profiles == {ALICE: profile}
