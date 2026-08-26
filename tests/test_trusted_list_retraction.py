"""Retraction planning (AC13) and the publish-failure safety rule (AC14)."""
from __future__ import annotations

from app.services.trusted_list_service import plan_retractions


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

    # Case 1: nothing ingested.
    monkeypatch.setattr(svc, "count_taggings_on_db", AsyncMock(return_value=0))
    r1 = asyncio.run(svc.generate_trusted_lists_for_observer("a" * 64))
    assert r1.retracted == 0

    # Case 2: taggings present, but the Observer has no qualifying asserters.
    monkeypatch.setattr(svc, "count_taggings_on_db", AsyncMock(return_value=9))
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
