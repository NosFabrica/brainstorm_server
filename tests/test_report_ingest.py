"""kind-1984 report ingestion: only user-level reports affect the graph.

Per NIP-56 a report against a note/media carries an `e` tag; a report against a
user is `p`-only. Only `e`-free reports produce REPORTS edges / reported_by: sets.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.message_queue_tasks import process_strfry_event as mod
from app.message_queue_tasks.process_strfry_event import (
    _extract_report_targets,
    process_event_kind_1984,
)


def _event(tags: list, pubkey: str = "r" * 64) -> dict:
    return {"pubkey": pubkey, "tags": tags, "content": ""}


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.run = AsyncMock()
    return session


def test_note_report_yields_no_targets():
    # e+p = reporting a specific note; must not count against the author.
    ev = _event([["e", "n" * 64, "spam"], ["p", "a" * 64]])
    assert _extract_report_targets(ev) == []


def test_user_report_yields_the_reported_pubkey():
    # p-only = reporting the user themselves.
    ev = _event([["p", "a" * 64, "nudity"]])
    assert _extract_report_targets(ev) == ["a" * 64]


def test_multi_target_user_report_yields_all_pubkeys():
    ev = _event([["p", "a" * 64], ["p", "b" * 64]])
    assert _extract_report_targets(ev) == ["a" * 64, "b" * 64]


def test_note_only_report_yields_no_targets():
    # e with no p (malformed/note-only) still targets content, not a user.
    ev = _event([["e", "n" * 64, "illegal"]])
    assert _extract_report_targets(ev) == []


def test_media_report_yields_no_targets():
    # NIP-56 media report: x (blob hash) + its required e tag -> content, not user.
    ev = _event([["x", "h" * 64, "malware"], ["e", "n" * 64], ["p", "a" * 64]])
    assert _extract_report_targets(ev) == []


# --- handler level: note reports touch neither Neo4j nor the reverse-cache ------


def test_note_report_handler_writes_nothing(monkeypatch):
    reverse = AsyncMock()
    monkeypatch.setattr(mod, "_update_reverse_sets", reverse)
    session = _mock_session()

    ev = _event([["e", "n" * 64, "spam"], ["p", "a" * 64]])
    asyncio.run(process_event_kind_1984(session, ev))

    session.run.assert_not_called()
    reverse.assert_not_called()


def test_user_report_handler_writes_edge_and_reverse_set(monkeypatch):
    reverse = AsyncMock()
    monkeypatch.setattr(mod, "_update_reverse_sets", reverse)
    session = _mock_session()

    ev = _event([["p", "a" * 64, "nudity"]], pubkey="r" * 64)
    asyncio.run(process_event_kind_1984(session, ev))

    session.run.assert_awaited_once()
    assert "REPORTS" in session.run.await_args.args[0]
    assert session.run.await_args.kwargs["reported_pubkeys"] == ["a" * 64]
    reverse.assert_awaited_once()
    assert reverse.await_args.kwargs["added_pubkeys"] == ["a" * 64]
