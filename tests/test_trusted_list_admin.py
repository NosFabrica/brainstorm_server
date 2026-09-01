"""Admin trigger endpoint (AC11, AC12) and result diagnosability (AC15).

Auth and routing are asserted against the real app; the service itself is
patched, so these stay in the fast suite. The service's own empty-path logic is
exercised directly with the repo boundary patched.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from nostr_sdk import Keys

from app.api import app
from app.routers.admin.router import verify_admin_access
from app.services import trusted_list_service as svc
from app.services.trusted_list_service import (
    EMPTY_REASON_NO_QUALIFYING_ASSERTERS,
    EMPTY_REASON_NO_TAGGINGS,
    TagResult,
    TrustedListRunResult,
)

OBSERVER = "a" * 64
SIGNER = "f" * 64


@pytest.fixture
def admin_client(client):
    app.dependency_overrides[verify_admin_access] = lambda: None
    yield client


def _run_result(**over):
    base = dict(
        observer=OBSERVER,
        signing_pubkey=SIGNER,
        taggings_in_store=5,
        qualifying_asserters=2,
        dictionary_size=1,
        published=1,
        failed=0,
        retracted=0,
        empty_reason=None,
        tags=[
            TagResult(
                slug="podcaster",
                d_tag="tl-tag-aaaaaaaa-bbbbbbbb-podcaster",
                tag_event_id="c" * 64,
                status="published",
                taggings_considered=4,
                member_count=2,
            )
        ],
    )
    base.update(over)
    return TrustedListRunResult(**base)


# --- AC11 ------------------------------------------------------------------


def test_trigger_succeeds_for_admin(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.trusted_lists.router.generate_trusted_lists_for_observer",
        AsyncMock(return_value=_run_result()),
    )
    resp = admin_client.post(f"/admin/trustedLists/{OBSERVER}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["observer"] == OBSERVER
    assert data["published"] == 1


def test_trigger_rejects_authenticated_non_admin(client):
    # `client` authenticates the caller but leaves the real admin gate in place.
    resp = client.post(f"/admin/trustedLists/{OBSERVER}")
    assert resp.status_code == 403


def test_trigger_rejects_malformed_observer(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.trusted_lists.router.generate_trusted_lists_for_observer",
        AsyncMock(return_value=_run_result()),
    )
    assert admin_client.post("/admin/trustedLists/not-a-pubkey").status_code == 400


def test_trigger_targets_path_observer_not_caller(admin_client, monkeypatch, caller):
    """The admin acts ON BEHALF OF a customer — the Observer is the path
    parameter, never the caller's own pubkey."""
    spy = AsyncMock(return_value=_run_result())
    monkeypatch.setattr(
        "app.routers.admin.trusted_lists.router.generate_trusted_lists_for_observer",
        spy,
    )
    admin_client.post(f"/admin/trustedLists/{OBSERVER}")
    spy.assert_awaited_once_with(OBSERVER)
    assert spy.await_args.args[0] != caller.pubkey


# --- AC12 / AC15 -----------------------------------------------------------


def test_empty_dictionary_publishes_nothing(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.trusted_lists.router.generate_trusted_lists_for_observer",
        AsyncMock(
            return_value=_run_result(
                taggings_in_store=0,
                qualifying_asserters=0,
                dictionary_size=0,
                published=0,
                tags=[],
                empty_reason=EMPTY_REASON_NO_TAGGINGS,
            )
        ),
    )
    resp = admin_client.post(f"/admin/trustedLists/{OBSERVER}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["published"] == 0
    assert data["tags"] == []


def test_response_reports_per_tag_counts(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.trusted_lists.router.generate_trusted_lists_for_observer",
        AsyncMock(return_value=_run_result()),
    )
    tag = admin_client.post(f"/admin/trustedLists/{OBSERVER}").json()["data"]["tags"][0]
    # A dictionary that quietly shrank between runs must be visible here, not
    # only by diffing published events.
    assert tag["taggings_considered"] == 4
    assert tag["member_count"] == 2
    assert tag["slug"] == "podcaster"


def _patch_service_reads(monkeypatch, *, taggings, asserters, qualifying):
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
    monkeypatch.setattr(svc, "count_taggings_on_db", AsyncMock(return_value=taggings))
    monkeypatch.setattr(
        svc, "get_asserter_pubkeys_on_db", AsyncMock(return_value=asserters)
    )

    @asynccontextmanager
    async def fake_neo():
        yield MagicMock()

    monkeypatch.setattr(svc.neo4j_driver, "session", fake_neo)
    # The repo returns `{asserter: trust weight}` since D12. Callers here pass
    # a bare list of pubkeys; give each a uniform mid weight, which scores any
    # single application well clear of the `score >= 1` floor.
    weights = (
        qualifying
        if isinstance(qualifying, dict)
        else {pubkey: 0.5 for pubkey in qualifying}
    )
    monkeypatch.setattr(
        svc,
        "get_qualifying_asserters_for_observer",
        AsyncMock(return_value=weights),
    )


def test_empty_result_distinguishes_empty_store_from_no_qualifiers(monkeypatch):
    """AC15: the two emptinesses must not look alike.

    An un-synced relay (nothing ingested) and a populated store where nobody
    cleared the rank threshold both publish zero lists — but they are different
    problems, and only one of them is an operator's to fix.
    """
    _patch_service_reads(monkeypatch, taggings=0, asserters=[], qualifying=[])
    empty_store = asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    assert empty_store.empty_reason == EMPTY_REASON_NO_TAGGINGS
    assert empty_store.taggings_in_store == 0

    _patch_service_reads(monkeypatch, taggings=7, asserters=["b" * 64], qualifying=[])
    no_qualifiers = asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    assert no_qualifiers.empty_reason == EMPTY_REASON_NO_QUALIFYING_ASSERTERS
    assert no_qualifiers.taggings_in_store == 7
    assert empty_store.empty_reason != no_qualifiers.empty_reason


def test_unscored_observer_yields_empty_dictionary(monkeypatch):
    """An Observer who has never been scored has no influence properties, so no
    asserter qualifies. That is an empty result, not a crash — and it is
    reported as such rather than as an empty store."""
    _patch_service_reads(
        monkeypatch, taggings=3, asserters=["b" * 64, "c" * 64], qualifying=[]
    )
    result = asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    assert result.published == 0
    assert result.tags == []
    assert result.empty_reason == EMPTY_REASON_NO_QUALIFYING_ASSERTERS


def test_signing_pubkey_is_derived_from_the_observers_own_nsec(monkeypatch):
    """AC10 (partial, fast): whatever key the run reports, it comes from the
    Observer's stored nsec — not a global/service key."""

    keys = Keys.generate()
    fake_db = MagicMock()

    @asynccontextmanager
    async def fake_session():
        yield fake_db

    nsec_row = MagicMock()
    nsec_row.nsec = keys.secret_key().to_bech32()
    monkeypatch.setattr(svc, "db_session", fake_session)
    monkeypatch.setattr(
        svc,
        "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(return_value=(nsec_row, False)),
    )
    monkeypatch.setattr(svc, "count_taggings_on_db", AsyncMock(return_value=0))
    monkeypatch.setattr(svc, "get_asserter_pubkeys_on_db", AsyncMock(return_value=[]))

    result = asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    assert result.signing_pubkey == keys.public_key().to_hex()


# --- I9 / I10: read-failure must abort BEFORE any publish ------------------
#
# Planned in the test plan as integration handles; implemented here as fast
# handles because the load-bearing assertion is "zero publishes happened", which
# is observable by patching the publish boundary and needs no live backend.
# Level change recorded in the test plan's coverage map.


def _patch_publish_spy(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(svc, "_publish", spy)
    monkeypatch.setattr(svc, "_connect", AsyncMock(return_value=MagicMock()))
    return spy


def test_db_failure_aborts_before_publishing(monkeypatch):
    """Postgres is source-of-truth. If it fails we must raise, never proceed —
    a silently empty dictionary would publish signed claims that people belong
    to nothing, and would then retract every live list as stale."""
    spy = _patch_publish_spy(monkeypatch)
    _patch_service_reads(monkeypatch, taggings=5, asserters=["b" * 64], qualifying=[])
    monkeypatch.setattr(
        svc, "count_taggings_on_db", AsyncMock(side_effect=RuntimeError("db down"))
    )
    with pytest.raises(RuntimeError):
        asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    spy.assert_not_awaited()


def test_neo4j_failure_aborts_before_publishing(monkeypatch):
    """Same rule for the rank read: 'no score' must never be silently treated
    as 'below threshold'."""
    spy = _patch_publish_spy(monkeypatch)
    _patch_service_reads(monkeypatch, taggings=5, asserters=["b" * 64], qualifying=[])
    monkeypatch.setattr(
        svc,
        "get_qualifying_asserters_for_observer",
        AsyncMock(side_effect=RuntimeError("neo4j down")),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    spy.assert_not_awaited()


# --- U13: one tag's publish failure must not abort the rest (AC14) ---------


def test_publish_failure_is_isolated_per_tag(monkeypatch):
    """Two tags in the dictionary; the FIRST publish blows up. The second tag
    must still publish, the failure must be reported per-tag with its error,
    and the failed tag's slot must stay current so retraction can't wipe it."""
    from app.repos.tagging_repo import DictionaryEntry

    _patch_service_reads(
        monkeypatch, taggings=4, asserters=["b" * 64], qualifying=["b" * 64]
    )
    entries = [
        DictionaryEntry(
            tag_event_id="c" * 64,
            tag_author_pubkey="d" * 64,
            slug="podcaster",
            name="Podcaster",
            description="",
            uses=2,
        ),
        DictionaryEntry(
            tag_event_id="e" * 64,
            tag_author_pubkey="d" * 64,
            slug="chef",
            name="Chef",
            description="",
            uses=1,
        ),
    ]
    monkeypatch.setattr(svc, "get_dictionary_on_db", AsyncMock(return_value=entries))
    monkeypatch.setattr(
        svc,
        "get_taggings_for_tag_on_db",
        AsyncMock(return_value=[("9" * 64, 1.0, "b" * 64)]),
    )
    monkeypatch.setattr(svc, "_connect", AsyncMock(return_value=MagicMock()))
    publish = AsyncMock(side_effect=[RuntimeError("relay rejected"), None])
    monkeypatch.setattr(svc, "_publish", publish)
    # Empty relay read-back: nothing previously published, nothing to retract.
    monkeypatch.setattr(svc, "_fetch_published_tl_slots", AsyncMock(return_value={}))

    result = asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))

    assert publish.await_count == 2  # the second tag was still attempted
    assert result.published == 1
    assert result.failed == 1
    by_slug = {t.slug: t for t in result.tags}
    assert by_slug["podcaster"].status == "failed"
    assert "relay rejected" in by_slug["podcaster"].error
    assert by_slug["chef"].status == "published"
    # AC14's other half: the failed slot is still a current d-tag, so a
    # hypothetical stale set containing it would NOT be retracted.
    assert (
        svc.plan_retractions(
            [by_slug["podcaster"].d_tag], {t.d_tag for t in result.tags}
        )
        == []
    )


# --- U9's third case: unauthenticated → 401 (AC11) -------------------------


def test_trigger_rejects_unauthenticated_caller():
    """No Authorization at all — the raw app, no dependency overrides."""
    from fastapi.testclient import TestClient

    raw_client = TestClient(app)
    resp = raw_client.post(f"/admin/trustedLists/{OBSERVER}")
    assert resp.status_code == 401
