"""kind-0 profile extraction: content JSON + tags merge.

Covers docs/search-vs-tapestry.md §8.4.1 — newer clients mirror profile fields
as tags (and use camelCase), and an empty event must not wipe a good profile.
"""
from __future__ import annotations

import json

from app.message_queue_tasks.process_strfry_event import _extract_kind0_profile


def _event(content: str = "", tags: list | None = None) -> dict:
    return {"pubkey": "p" * 64, "content": content, "tags": tags or []}


def test_content_json_is_extracted():
    ev = _event(content=json.dumps({"name": "alice", "about": "hi", "x": "ignored"}))
    out = _extract_kind0_profile(ev)
    assert out["name"] == "alice"
    assert out["about"] == "hi"
    assert "x" not in out  # only recognized PROFILE_FIELDS are kept


def test_tag_only_event_is_extracted_not_wiped():
    # Empty content but profile data in tags — must NOT be empty (the wipe bug).
    ev = _event(
        content="",
        tags=[["alt", "..."], ["name", "bob"], ["nip05", "bob@example.com"]],
    )
    out = _extract_kind0_profile(ev)
    assert out["name"] == "bob"
    assert out["nip05"] == "bob@example.com"


def test_content_wins_over_tags_on_conflict():
    ev = _event(
        content=json.dumps({"name": "from_content"}),
        tags=[["name", "from_tag"], ["website", "https://w"]],
    )
    out = _extract_kind0_profile(ev)
    assert out["name"] == "from_content"  # content is the base
    assert out["website"] == "https://w"  # tags fill the gap


def test_camelcase_displayname_is_normalized():
    ev = _event(content=json.dumps({"displayName": "Carol"}))
    out = _extract_kind0_profile(ev)
    assert out["display_name"] == "Carol"
    assert "displayName" not in out


def test_canonical_name_wins_over_deprecated_username():
    # Both present: `name` is canonical (NIP-24), so `username` is dropped.
    ev = _event(content=json.dumps({"username": "dave", "name": "Dave"}))
    out = _extract_kind0_profile(ev)
    assert out["name"] == "Dave"
    assert "username" not in out


def test_deprecated_username_backfills_missing_name():
    # Only the deprecated alias present → it populates the canonical `name`.
    ev = _event(content=json.dumps({"username": "dave"}))
    out = _extract_kind0_profile(ev)
    assert out["name"] == "dave"
    assert "username" not in out


def test_blank_canonical_falls_back_to_deprecated():
    # An empty/whitespace `name` is treated as missing → `username` backfills it.
    ev = _event(content=json.dumps({"name": "   ", "username": "dave"}))
    out = _extract_kind0_profile(ev)
    assert out["name"] == "dave"


def test_empty_or_malformed_event_yields_no_fields():
    assert _extract_kind0_profile(_event(content="")) == {}
    assert _extract_kind0_profile(_event(content="not json")) == {}
    assert _extract_kind0_profile(_event(content=json.dumps({"x": "y"}))) == {}
