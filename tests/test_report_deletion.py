"""kind-5 (NIP-09) reactive retraction of user reports.

strfry deletes the referenced kind-1984 from LMDB *before* the kind-5 reaches
redis, so the retracted report is unreadable by the time we see the deletion.
The only sound move is to recompute the deleting author's *surviving* user-level
report set from the relay and reconcile against it. Relay/Neo4j/Redis I/O is
verified at the handler level and per-env, not here.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.message_queue_tasks import process_strfry_event as mod
from app.message_queue_tasks.process_strfry_event import process_event_kind_5
from app.services.report_graph_service import (
    diff_author_targets,
    surviving_report_targets,
)

AUTHOR = "r" * 64


def _report(targets: list[str], author: str = AUTHOR, eid: str = "id0") -> dict:
    return {
        "id": eid,
        "pubkey": author,
        "kind": 1984,
        "tags": [["p", t] for t in targets],
        "content": "",
    }


def _deletion(k: list[str] | None = ["1984"], author: str = AUTHOR) -> dict:
    tags: list[list[str]] = [["e", "n" * 64]]
    if k is not None:
        tags += [["k", v] for v in k]
    return {"id": "del", "pubkey": author, "kind": 5, "tags": tags, "content": ""}


# --- surviving_report_targets: author-scoped, user-only -------------------------


def test_surviving_targets_are_the_authors_user_report_targets():
    events = [_report(["a" * 64], eid="r1"), _report(["b" * 64], eid="r2")]
    assert surviving_report_targets(events, AUTHOR) == {"a" * 64, "b" * 64}


def test_surviving_targets_exclude_note_reports():
    # A note report (has an `e` tag) never counted -> must not resurrect here.
    note = _report(["a" * 64], eid="r1")
    note["tags"].insert(0, ["e", "n" * 64])
    assert surviving_report_targets([note], AUTHOR) == set()


def test_surviving_targets_ignore_other_authors_reports():
    # Defensive: the relay filter is authors=[X], but a stray event must not
    # leak into X's set and get attributed to X.
    other = _report(["z" * 64], author="q" * 64, eid="r9")
    assert surviving_report_targets([other], AUTHOR) == set()


def test_no_surviving_reports_yields_empty_set():
    # The author deleted their only report: empty is a real answer, not an error.
    assert surviving_report_targets([], AUTHOR) == set()


def test_two_reports_on_one_target_yield_one_surviving_target():
    events = [_report(["a" * 64], eid="r1"), _report(["a" * 64], eid="r2")]
    assert surviving_report_targets(events, AUTHOR) == {"a" * 64}


# --- diff_author_targets: X's outgoing edges, desired vs current ----------------


def test_retracted_target_is_scheduled_for_removal():
    diff = diff_author_targets(desired={"a" * 64}, current={"a" * 64, "b" * 64})
    assert diff.to_remove == {"b" * 64}
    assert diff.to_add == set()


def test_missing_target_is_scheduled_for_addition():
    diff = diff_author_targets(desired={"a" * 64, "b" * 64}, current={"a" * 64})
    assert diff.to_add == {"b" * 64}
    assert diff.to_remove == set()


def test_unchanged_state_yields_empty_diff():
    diff = diff_author_targets(desired={"a" * 64}, current={"a" * 64})
    assert not diff


def test_deleting_the_only_report_removes_every_target():
    diff = diff_author_targets(desired=set(), current={"a" * 64})
    assert diff.to_remove == {"a" * 64}
    assert bool(diff) is True


def test_multi_report_survival_keeps_the_target():
    # X reported `a` twice and deleted one: `a` still survives -> no edge churn.
    surviving = surviving_report_targets([_report(["a" * 64], eid="r2")], AUTHOR)
    diff = diff_author_targets(desired=surviving, current={"a" * 64})
    assert not diff


# --- handler: recompute -> reconcile Neo4j edges + reported_by: sets ------------


def _mock_session(current_targets: list[str]) -> MagicMock:
    """Neo4j session whose REPORTS read returns `current_targets`."""
    session = MagicMock()
    result = MagicMock()
    result.single = AsyncMock(return_value={"targets": current_targets})
    session.run = AsyncMock(return_value=result)
    return session


def _cyphers(session: MagicMock) -> str:
    """Every cypher the handler ran, concatenated — for write assertions."""
    return "\n".join(call.args[0] for call in session.run.await_args_list)


@pytest.fixture
def reverse(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(mod, "_update_reverse_sets", mock)
    return mock


def _patch_fetch(monkeypatch, result):
    fetch = AsyncMock(return_value=result)
    monkeypatch.setattr(mod, "fetch_author_user_reports", fetch)
    return fetch


def test_deletion_of_only_report_removes_edge_and_reverse_member(monkeypatch, reverse):
    # The acceptance case: X retracts their one report on `a`. The relay has
    # already purged it, so the recompute comes back empty.
    _patch_fetch(monkeypatch, [])
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert "DELETE" in _cyphers(session)
    reverse.assert_awaited_once()
    assert reverse.await_args.kwargs["removed_pubkeys"] == ["a" * 64]
    assert reverse.await_args.kwargs["added_pubkeys"] == []


def test_surviving_second_report_keeps_edge_and_member(monkeypatch, reverse):
    # X reported `a` twice, deletes one. `a` is still reported -> edge stays.
    _patch_fetch(monkeypatch, [_report(["a" * 64], eid="r2")])
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert "DELETE" not in _cyphers(session)
    assert reverse.await_args.kwargs["removed_pubkeys"] == []


def test_only_the_retracted_target_is_removed(monkeypatch, reverse):
    # X reported `a` and `b`, retracts only the one on `b`.
    _patch_fetch(monkeypatch, [_report(["a" * 64], eid="r1")])
    session = _mock_session(current_targets=["a" * 64, "b" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert reverse.await_args.kwargs["removed_pubkeys"] == ["b" * 64]


def test_k_tag_is_not_a_gate(monkeypatch, reverse):
    # strfry deletes on e-tag + author without reading `k`, so no `k` value rules
    # a report deletion out. Gating on it stranded a live edge (2026-07-17).
    for k in (["1"], None):
        fetch = _patch_fetch(monkeypatch, [])
        session = _mock_session(current_targets=["a" * 64])

        asyncio.run(process_event_kind_5(session, _deletion(k=k)))

        fetch.assert_awaited_once()
        assert reverse.await_args.kwargs["removed_pubkeys"] == ["a" * 64]


def test_unreadable_relay_reconciles_nothing(monkeypatch, reverse):
    # None = "we don't know". Treating it as "no reports" would wipe live edges.
    _patch_fetch(monkeypatch, None)
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert "DELETE" not in _cyphers(session) and "MERGE" not in _cyphers(session)
    reverse.assert_not_called()


def test_recompute_adds_a_target_missing_from_the_graph(monkeypatch, reverse):
    # The recompute is authoritative both ways: a report the graph never got
    # (dropped event, past outage) is restored by the same reconcile.
    _patch_fetch(monkeypatch, [_report(["a" * 64, "b" * 64], eid="r1")])
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert "MERGE" in _cyphers(session)
    assert reverse.await_args.kwargs["added_pubkeys"] == ["a" * 64, "b" * 64]


def test_author_with_no_report_edges_never_hits_the_relay(monkeypatch, reverse):
    # kind-5 is high volume once pulled from remote relays and most of its
    # authors have never reported anyone. Their deletion can't remove an edge we
    # don't have, so it must not cost a websocket REQ.
    fetch = _patch_fetch(monkeypatch, [])
    session = _mock_session(current_targets=[])

    asyncio.run(process_event_kind_5(session, _deletion()))

    fetch.assert_not_called()
    reverse.assert_not_called()


def test_note_report_survivor_is_not_credited_as_a_user_report(monkeypatch, reverse):
    # The surviving event is a note report -> it must not hold the edge open.
    note = _report(["a" * 64], eid="r1")
    note["tags"].insert(0, ["e", "n" * 64])
    _patch_fetch(monkeypatch, [note])
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert reverse.await_args.kwargs["removed_pubkeys"] == ["a" * 64]


def test_unchanged_graph_writes_no_edges(monkeypatch, reverse):
    _patch_fetch(monkeypatch, [_report(["a" * 64], eid="r1")])
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    assert "DELETE" not in _cyphers(session) and "MERGE" not in _cyphers(session)


def test_reported_by_is_reasserted_even_when_neo4j_already_matches(
    monkeypatch, reverse
):
    # Redis is the store GrapeRank reads and it drifts from Neo4j on its own —
    # the backfill writes Neo4j then Redis non-atomically, so a crash between
    # them leaves exactly this state. Gating the SADD on the Neo4j diff would
    # leave the drifted store unrepaired forever.
    _patch_fetch(monkeypatch, [_report(["a" * 64], eid="r1")])
    session = _mock_session(current_targets=["a" * 64])

    asyncio.run(process_event_kind_5(session, _deletion()))

    reverse.assert_awaited_once()
    assert reverse.await_args.kwargs["added_pubkeys"] == ["a" * 64]


def test_dispatcher_routes_kind_5_to_the_handler(monkeypatch):
    # Without this wiring the whole feature is dead code.
    handler = AsyncMock()
    monkeypatch.setattr(mod, "process_event_kind_5", handler)
    session = _mock_session(current_targets=[])
    ev = _deletion()

    asyncio.run(mod.process_strfry_event(session, ev))

    handler.assert_awaited_once_with(session, ev)
