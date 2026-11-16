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


@pytest.fixture(autouse=True)
def _no_event_taggings(monkeypatch):
    """Story 1's tests run with no event taggings held, so the content lists
    (ADR trusted-lists/0002) stay out of their way. Story 2's tests override."""
    monkeypatch.setattr(svc, "count_event_taggings_on_db", AsyncMock(return_value=0))
    monkeypatch.setattr(
        svc, "get_event_tagging_asserters_on_db", AsyncMock(return_value=[])
    )


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


# ===========================================================================
# Story 2: Trusted Lists of tagged content (ADR trusted-lists/0002 D8)
#
# The run with its boundaries patched: the reads, the lookups' one network
# call (`_fetch_from_relay`, faked by in-memory relays), the publish, and the
# two live-slot scans. Every assertion is on what was published or retracted.
# ===========================================================================

from app.repos.event_tagging_repo import EventTaggingRow  # noqa: E402
from app.repos.tagging_repo import DictionaryEntry  # noqa: E402
from app.services import tagging_target_service as tts  # noqa: E402
from app.services.trusted_list_service import ContentSlot  # noqa: E402

ASSERTER = "b" * 64
TAG_AUTHOR = "d" * 64
NOTE = "1" * 64
LOOKUP = "wss://lookup.example"
HINT = "wss://hint.example"
NOTE_D = "tl-tag-notes-aaaaaaaa-dddddddd-espresso-k1"


def _row(target=NOTE, slot="e", relay=None, polarity=1.0):
    return EventTaggingRow(
        asserter_pubkey=ASSERTER,
        target=target,
        target_slot=slot,
        target_relay=relay,
        polarity=polarity,
        created_at_unix=1,
        tag_event_id="c" * 64,
        tag_author_pubkey=TAG_AUTHOR,
        slug="espresso",
        name="espresso",
        description="",
    )


def _note_event(id_=NOTE, kind=1):
    return {"id": id_, "kind": kind, "pubkey": "e" * 64, "created_at": 1, "tags": []}


def _people_entry(slug="podcaster"):
    return DictionaryEntry(
        tag_event_id="c" * 64,
        tag_author_pubkey=TAG_AUTHOR,
        slug=slug,
        name=slug,
        description="",
        uses=1,
    )


def _content_slot(d=NOTE_D, kind=30393, **over):
    base = dict(
        kind=kind,
        d_tag=d,
        retracted=False,
        title="espresso Notes",
        source_tag=["source-tag", f"39999:{TAG_AUTHOR}:espresso", TAG_AUTHOR, "espresso"],
        tagging_target="1",
    )
    base.update(over)
    return ContentSlot(**base)


def _content_run(
    monkeypatch,
    *,
    rows,
    stores=None,
    down=(),
    configured=(LOOKUP,),
    people=(),
    live_people=None,
    live_content=(),
    people_taggings=None,
    slot_scan=None,
    publish_fails=(),
):
    """Run the service once; returns (result, published, cached)."""
    from app.core.config import settings

    people_taggings = (1 if people else 0) if people_taggings is None else people_taggings
    _patch_service_reads(
        monkeypatch, taggings=people_taggings, asserters=[ASSERTER], qualifying=[ASSERTER]
    )
    monkeypatch.setattr(svc, "count_event_taggings_on_db", AsyncMock(return_value=len(rows)))
    monkeypatch.setattr(
        svc, "get_event_tagging_asserters_on_db", AsyncMock(return_value=[ASSERTER])
    )
    monkeypatch.setattr(svc, "get_event_taggings_on_db", AsyncMock(return_value=list(rows)))
    monkeypatch.setattr(svc, "get_tagged_events_on_db", AsyncMock(return_value={}))
    cached = AsyncMock()
    monkeypatch.setattr(svc, "insert_tagged_events_on_db", cached)
    monkeypatch.setattr(svc, "get_dictionary_on_db", AsyncMock(return_value=list(people)))
    monkeypatch.setattr(
        svc,
        "get_taggings_for_tag_on_db",
        AsyncMock(return_value=[("9" * 64, 1.0, ASSERTER)]),
    )
    monkeypatch.setattr(settings, "trusted_list_target_relays", list(configured))

    async def fetch(url, filters):
        if url in down:
            raise ConnectionError("unreachable")
        return list((stores or {}).get(url, []))

    monkeypatch.setattr(tts, "_fetch_from_relay", fetch)
    monkeypatch.setattr(svc, "_connect", AsyncMock(return_value=MagicMock()))

    published: list[tuple[int, list[list[str]]]] = []

    async def publish(_client, tags, _content, kind=30392):
        published.append((kind, tags))
        d = next(t[1] for t in tags if t[0] == "d")
        if d in publish_fails:
            raise RuntimeError("relay rejected")

    monkeypatch.setattr(svc, "_publish", publish)
    monkeypatch.setattr(
        svc,
        "_fetch_published_tl_slots",
        slot_scan or AsyncMock(return_value=dict(live_people or {})),
    )
    monkeypatch.setattr(
        svc, "_fetch_published_content_slots", AsyncMock(return_value=list(live_content))
    )
    result = asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    return result, published, cached


def _retractions(published):
    return [(k, t) for k, t in published if ["status", "retracted"] in t]


def _kinds(published):
    return sorted({k for k, t in published if ["status", "retracted"] not in t})


# --- AC14 / U13: one run, three kinds, new result fields ----------------------


def test_one_run_builds_three_kinds_and_reports_new_fields(monkeypatch):
    article = "30023:" + "e" * 64 + ":essay"
    result, published, _ = _content_run(
        monkeypatch,
        rows=[_row(), _row(article, slot="a"), _row("7" * 64)],
        stores={LOOKUP: [_note_event(), _note_event("7" * 64, kind=7)]},
        people=[_people_entry()],
    )
    assert _kinds(published) == [30392, 30393, 30394]
    by_kind = {t.kind: t for t in result.tags}
    assert by_kind[30393].tagging_target == "1"
    assert by_kind[30394].tagging_target == "30023"
    assert by_kind[30392].tagging_target is None
    assert result.unnamed_kinds == [7]
    assert result.unresolved_targets == 0
    assert result.content_skipped_reason is None
    assert result.event_taggings_in_store == 3


def test_the_response_carries_the_new_fields(admin_client, monkeypatch):
    run = _run_result(
        event_taggings_in_store=3,
        unresolved_targets=2,
        unnamed_kinds=[7],
        tags=[
            TagResult(
                slug="espresso",
                d_tag=NOTE_D,
                tag_event_id="c" * 64,
                status="published",
                kind=30393,
                tagging_target="1",
            )
        ],
    )
    monkeypatch.setattr(
        "app.routers.admin.trusted_lists.router.generate_trusted_lists_for_observer",
        AsyncMock(return_value=run),
    )
    data = admin_client.post(f"/admin/trustedLists/{OBSERVER}").json()["data"]
    assert (data["unresolved_targets"], data["unnamed_kinds"]) == (2, [7])
    assert data["event_taggings_in_store"] == 3
    assert data["content_skipped_reason"] is None
    assert (data["tags"][0]["kind"], data["tags"][0]["tagging_target"]) == (30393, "1")


def test_unresolved_target_is_counted_and_not_cached(monkeypatch):
    """AC7: a target no relay has joins no list, is counted, and isn't cached,
    so the next run looks it up again. What was found is cached."""
    result, published, cached = _content_run(
        monkeypatch,
        rows=[_row(), _row("2" * 64)],
        stores={LOOKUP: [_note_event()]},
    )
    assert result.unresolved_targets == 1
    (stored,) = cached.await_args.args[1:]
    assert [e.event_id for e in stored] == [NOTE]
    (note_list,) = [t for k, t in published if k == 30393]
    assert [t[1] for t in note_list if t[0] == "e"] == [NOTE]


# --- AC8: a failed lookup ----------------------------------------------------


def test_unreachable_lookup_relays_skip_content_lists_only(monkeypatch):
    result, published, _ = _content_run(
        monkeypatch,
        rows=[_row()],
        down={LOOKUP},
        people=[_people_entry()],
        live_people={"tl-tag-aaaaaaaa-dddddddd-gone": False},
        live_content=[_content_slot("tl-tag-notes-aaaaaaaa-dddddddd-gone-k1")],
    )
    assert result.content_skipped_reason.startswith("target_lookup_failed")
    assert _kinds(published) == [30392]
    # S1, one direction: the stale people list is still retracted, no content one is.
    assert [k for k, _ in _retractions(published)] == [30392]


def test_one_configured_lookup_relay_down_is_a_failed_lookup(monkeypatch):
    """The search relay down, a hint relay answering every target: still a
    failed lookup, so the live list isn't republished emptied."""
    result, published, _ = _content_run(
        monkeypatch,
        rows=[_row(relay=HINT)],
        configured=(LOOKUP, "wss://second.example"),
        down={"wss://second.example"},
        stores={LOOKUP: [], HINT: [_note_event()]},
        live_content=[_content_slot()],
    )
    assert result.content_skipped_reason is not None
    assert [k for k, _ in published if k != 30392] == []


def test_hint_relay_down_leaves_the_view_trusted(monkeypatch):
    result, published, _ = _content_run(
        monkeypatch,
        rows=[_row(relay=HINT)],
        down={HINT},
        stores={LOOKUP: [_note_event()]},
    )
    assert result.content_skipped_reason is None
    assert _kinds(published) == [30393]


# --- AC15 / AC16 / S1: retraction across kinds ----------------------------


def test_stale_content_slot_is_retracted_with_its_live_identity(monkeypatch):
    stale = _content_slot(
        "tl-tag-items-aaaaaaaa-dddddddd-a-b-dlcccccccc-x-y",
        kind=30394,
        title="a-b Books",
        source_tag=["source-tag", f"39999:{TAG_AUTHOR}:a-b", TAG_AUTHOR, "a-b"],
        tagging_target=f"39998:{'c' * 64}:x-y",
    )
    _, published, _ = _content_run(
        monkeypatch,
        rows=[_row()],
        stores={LOOKUP: [_note_event()]},
        live_content=[_content_slot(), stale],
    )
    ((kind, tags),) = _retractions(published)
    assert kind == 30394
    assert ["title", "a-b Books"] in tags
    assert ["tagging-target", f"39998:{'c' * 64}:x-y"] in tags
    assert ["source-tag", f"39999:{TAG_AUTHOR}:a-b", TAG_AUTHOR, "a-b"] in tags
    assert [t for t in tags if t[0] in ("a", "e", "p")] == []


def test_unreadable_live_slot_is_left_while_a_readable_one_is_retracted(monkeypatch):
    _, published, _ = _content_run(
        monkeypatch,
        rows=[_row()],
        stores={LOOKUP: [_note_event()]},
        live_content=[
            _content_slot("tl-tag-notes-aaaaaaaa-dddddddd-old-k1"),
            _content_slot("tl-tag-notes-aaaaaaaa-dddddddd-garbled-k1", source_tag=None),
        ],
    )
    retracted = [next(t[1] for t in tags if t[0] == "d") for _, tags in _retractions(published)]
    assert retracted == ["tl-tag-notes-aaaaaaaa-dddddddd-old-k1"]


def test_failed_slot_scan_retracts_nothing_across_kinds(monkeypatch):
    article = "30023:" + "e" * 64 + ":essay"
    _, published, _ = _content_run(
        monkeypatch,
        rows=[_row(), _row(article, slot="a")],
        stores={LOOKUP: [_note_event()]},
        people=[_people_entry()],
        slot_scan=AsyncMock(side_effect=RuntimeError("relay gone")),
        live_content=[_content_slot("tl-tag-notes-aaaaaaaa-dddddddd-old-k1")],
    )
    assert _kinds(published) == [30392, 30393, 30394]
    assert _retractions(published) == []


def test_emptied_content_list_republished_without_marker(monkeypatch):
    """The pair is still in the dictionary (one dispute is a use) but has no
    member: republished empty, not retracted (AC16)."""
    _, published, _ = _content_run(
        monkeypatch,
        rows=[_row(polarity=-1.0)],
        stores={LOOKUP: [_note_event()]},
        live_content=[_content_slot()],
    )
    ((kind, tags),) = published
    assert kind == 30393
    assert [t for t in tags if t[0] == "e"] == []
    assert ["status", "retracted"] not in tags


def test_retraction_scans_are_kind_scoped(monkeypatch):
    """S1, the other direction: a trusted, empty people dictionary retracts a
    stale 30392 and leaves the current content list alone."""
    _, published, _ = _content_run(
        monkeypatch,
        rows=[_row()],
        stores={LOOKUP: [_note_event()]},
        people_taggings=4,
        live_people={"tl-tag-aaaaaaaa-dddddddd-gone": False},
        live_content=[_content_slot()],
    )
    assert [k for k, _ in _retractions(published)] == [30392]
    assert _kinds(published) == [30393]


def test_failed_content_publish_is_isolated_and_keeps_its_slot(monkeypatch):
    article = "30023:" + "e" * 64 + ":essay"
    article_d = "tl-tag-items-aaaaaaaa-dddddddd-espresso-k30023"
    result, published, _ = _content_run(
        monkeypatch,
        rows=[_row(), _row(article, slot="a")],
        stores={LOOKUP: [_note_event()]},
        live_content=[_content_slot()],
        publish_fails={NOTE_D},
    )
    by_d = {t.d_tag: t for t in result.tags}
    assert by_d[NOTE_D].status == "failed"
    assert by_d[article_d].status == "published"
    assert _retractions(published) == []


# --- read failures abort before any publish, content included ---------------


def test_event_tagging_read_failure_aborts_before_publishing(monkeypatch):
    spy = _patch_publish_spy(monkeypatch)
    _patch_service_reads(monkeypatch, taggings=5, asserters=["b" * 64], qualifying=["b" * 64])
    monkeypatch.setattr(svc, "count_event_taggings_on_db", AsyncMock(return_value=3))
    monkeypatch.setattr(
        svc, "get_event_tagging_asserters_on_db", AsyncMock(return_value=["b" * 64])
    )
    monkeypatch.setattr(svc, "get_dictionary_on_db", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        svc, "get_event_taggings_on_db", AsyncMock(side_effect=RuntimeError("db down"))
    )
    with pytest.raises(RuntimeError):
        asyncio.run(svc.generate_trusted_lists_for_observer(OBSERVER))
    spy.assert_not_awaited()
