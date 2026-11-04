"""Retraction planning (AC13) and the publish-failure safety rule (AC14)."""
from __future__ import annotations

import pytest

from app.services.trusted_list_service import (
    ContentSlot,
    content_slot_of,
    plan_content_retractions,
    plan_retractions,
)


def test_stale_slot_is_retracted():
    assert plan_retractions(["tl-tag-a-b-old"], {"tl-tag-a-b-new"}) == [
        "tl-tag-a-b-old"
    ]


def test_current_slot_is_not_retracted():
    assert plan_retractions(["tl-tag-a-b-x"], {"tl-tag-a-b-x"}) == []


def test_failed_publish_keeps_dtag_current():
    """A tag whose publish failed is still in the current set, so its live TL
    survives. Tapestry learned this as B4a: a transient relay failure once
    caused the retraction sweep to wipe healthy lists.
    """
    current = {"tl-tag-a-b-ok", "tl-tag-a-b-failed"}
    assert plan_retractions(["tl-tag-a-b-ok", "tl-tag-a-b-failed"], current) == []


def test_only_slots_absent_from_current_are_retracted():
    published = ["keep-1", "keep-2", "drop-1", "drop-2"]
    assert plan_retractions(published, {"keep-1", "keep-2"}) == ["drop-1", "drop-2"]


def test_nothing_published_yet_retracts_nothing():
    assert plan_retractions([], {"tl-tag-a-b-x"}) == []


# --- U17: retraction only fires from a trustworthy view --------------------


def test_untrustworthy_empty_view_never_retracts(monkeypatch):
    """An empty result caused by BROKEN INPUT must retract nothing.

    Two of the three empty outcomes mean "our view is wrong", not "the tags went
    away": nothing ingested (an un-synced relay) and an Observer who was never
    scored. Retracting on either would wipe every live list that Observer has,
    on the strength of data we know is missing. Only an empty dictionary reached
    with taggings present AND qualifying asserters present is actionable.
    """
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from nostr_sdk import Keys

    from app.services import trusted_list_service as svc

    connect = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(svc, "_connect", connect)
    monkeypatch.setattr(svc, "_publish", publish)

    fake_db = MagicMock()

    @asynccontextmanager
    async def fake_session():
        yield fake_db

    nsec_row = MagicMock()
    nsec_row.nsec = Keys.generate().secret_key().to_bech32()
    monkeypatch.setattr(svc, "db_session", fake_session)
    monkeypatch.setattr(
        svc,
        "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(return_value=(nsec_row, False)),
    )
    monkeypatch.setattr(svc, "get_asserter_pubkeys_on_db", AsyncMock(return_value=[]))

    # Case 1: nothing ingested, of either kind of tagging.
    monkeypatch.setattr(svc, "count_taggings_on_db", AsyncMock(return_value=0))
    monkeypatch.setattr(svc, "count_event_taggings_on_db", AsyncMock(return_value=0))
    r1 = asyncio.run(svc.generate_trusted_lists_for_observer("a" * 64))
    assert r1.retracted == 0

    # Case 2: taggings present (pubkey and event taggings, ADR 0002), but the
    # Observer has no qualifying asserters.
    monkeypatch.setattr(svc, "count_taggings_on_db", AsyncMock(return_value=9))
    monkeypatch.setattr(svc, "count_event_taggings_on_db", AsyncMock(return_value=4))
    monkeypatch.setattr(
        svc, "get_event_tagging_asserters_on_db", AsyncMock(return_value=["c" * 64])
    )
    monkeypatch.setattr(
        svc, "get_asserter_pubkeys_on_db", AsyncMock(return_value=["b" * 64])
    )

    @asynccontextmanager
    async def fake_neo():
        yield MagicMock()

    monkeypatch.setattr(svc.neo4j_driver, "session", fake_neo)
    monkeypatch.setattr(
        svc, "get_qualifying_asserters_for_observer", AsyncMock(return_value=[])
    )
    r2 = asyncio.run(svc.generate_trusted_lists_for_observer("a" * 64))
    assert r2.retracted == 0

    # Neither case may even open a relay connection, let alone publish.
    connect.assert_not_awaited()
    publish.assert_not_awaited()


# --- story 2: content slots (ADR trusted-lists/0002 D8) ---------------------

OBSERVER8 = "aaaaaaaa"
SOURCE = ["source-tag", f"39999:{'d' * 64}:a-b", "d" * 64, "a-b"]


def _live(kind=30394, d=f"tl-tag-items-{OBSERVER8}-dddddddd-a-b-dlcccccccc-x-y", extra=()):
    return {
        "kind": kind,
        "tags": [
            ["d", d],
            ["title", "a-b Books"],
            SOURCE,
            ["tagging-target", f"39998:{'c' * 64}:x-y"],
            *extra,
        ],
    }


def test_content_slot_identity_is_read_off_the_live_event():
    """Slug `a-b` and DList `d` `x-y`: splitting the `d` would misread both."""
    slot = content_slot_of(_live())
    assert slot is not None and slot.readable
    assert slot.source_tag == SOURCE
    assert slot.tagging_target == f"39998:{'c' * 64}:x-y"
    assert slot.title == "a-b Books"
    assert not slot.retracted


def test_content_slot_of_skips_what_isnt_ours():
    assert content_slot_of(_live(kind=30392)) is None
    assert content_slot_of(_live(d="tl-pin-items-x")) is None
    # A 30393 must carry the notes prefix, a 30394 the items prefix.
    assert content_slot_of(_live(kind=30393)) is None


def test_retracted_content_slot_is_marked():
    slot = content_slot_of(_live(extra=[["status", "retracted"]]))
    assert slot is not None and slot.retracted


def _slot(d="x", kind=30394, **over):
    base = dict(
        kind=kind,
        d_tag=d,
        retracted=False,
        title="a-b Books",
        source_tag=SOURCE,
        tagging_target="1",
    )
    base.update(over)
    return ContentSlot(**base)


def test_content_slot_retracted_reading_identity_from_live_event():
    """AC15: stale = absent from this run's current set, per (kind, d)."""
    slots = [_slot("keep"), _slot("drop"), _slot("keep", kind=30393)]
    stale = plan_content_retractions(slots, {(30394, "keep")})
    assert [(s.kind, s.d_tag) for s in stale] == [(30394, "drop"), (30393, "keep")]


def test_failed_content_publish_keeps_its_slot():
    """A list whose publish failed is still current, so it isn't retracted."""
    current = {(30393, "ok"), (30393, "failed")}
    assert plan_content_retractions([_slot("failed", kind=30393)], current) == []


def test_already_retracted_content_slot_is_not_retracted_again():
    assert plan_content_retractions([_slot("old", retracted=True)], set()) == []


@pytest.mark.parametrize(
    "over",
    [
        dict(source_tag=None),
        dict(tagging_target=None),
        dict(title=None),
        dict(source_tag=["source-tag", "not-a-coordinate", "d" * 64, "a-b"]),
        dict(source_tag=["source-tag", f"39999:{'d' * 64}:a-b"]),
    ],
    ids=["no source-tag", "no tagging-target", "no title",
         "source-tag not a coordinate", "source-tag short"],
)
def test_unreadable_live_slot_is_left_in_place(over):
    assert not _slot(**over).readable
