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
