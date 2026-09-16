"""Storage semantics for the tagging tables (AC1, AC2).

Replaceability is a WRITE-TIME invariant, so these assertions can only be made
against a real database — the fast suite cannot see them.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db_models import NostrTagElement, NostrUserTagging
from app.repos.tagging_repo import (
    upsert_tag_element_on_db,
    upsert_user_tagging_on_db,
)
from app.services.tagging_parse import TagElement, UserTagging
from tests.integration.tagging_harness import run_with_db

pytestmark = pytest.mark.integration

AUTHOR = "a" * 64
ASSERTER = "b" * 64
TARGET = "c" * 64
TAGEV = "d" * 64


def _element(event_id, name, created_at, slug="podcaster", author=AUTHOR):
    return TagElement(
        event_id=event_id,
        author_pubkey=author,
        slug=slug,
        name=name,
        description="d",
        created_at_unix=created_at,
    )


def _tagging(event_id, polarity, created_at, d_tag="dt-1"):
    return UserTagging(
        event_id=event_id,
        asserter_pubkey=ASSERTER,
        d_tag=d_tag,
        target_pubkey=TARGET,
        tag_event_id=TAGEV,
        polarity=polarity,
        created_at_unix=created_at,
    )


# --- AC1 -------------------------------------------------------------------


def test_tag_element_upsert_is_latest_wins():
    async def work(db):
        await upsert_tag_element_on_db(db, _element("1" * 64, "Old", 100))
        await upsert_tag_element_on_db(db, _element("2" * 64, "New", 200))
        rows = (await db.execute(select(NostrTagElement))).scalars().all()
        return [(r.event_id, r.name) for r in rows]

    rows = run_with_db(work)
    assert len(rows) == 1
    assert rows[0] == ("2" * 64, "New")


def test_tag_element_older_event_does_not_overwrite():
    async def work(db):
        await upsert_tag_element_on_db(db, _element("2" * 64, "New", 200))
        await upsert_tag_element_on_db(db, _element("1" * 64, "Old", 100))
        rows = (await db.execute(select(NostrTagElement))).scalars().all()
        return [(r.name, r.created_at_unix) for r in rows]

    # Out-of-order delivery must not regress the row.
    assert run_with_db(work) == [("New", 200)]


def test_same_slug_by_different_authors_are_distinct_elements():
    async def work(db):
        await upsert_tag_element_on_db(db, _element("1" * 64, "Mine", 100))
        await upsert_tag_element_on_db(
            db, _element("2" * 64, "Theirs", 100, author="e" * 64)
        )
        rows = (await db.execute(select(NostrTagElement))).scalars().all()
        return sorted(r.name for r in rows)

    # tags.md: same slug by different authors are DISTINCT elements.
    assert run_with_db(work) == ["Mine", "Theirs"]


# --- AC2 -------------------------------------------------------------------


def test_tagging_upsert_latest_wins_across_polarity_flip():
    async def work(db):
        await upsert_user_tagging_on_db(db, _tagging("1" * 64, 1.0, 100))
        await upsert_user_tagging_on_db(db, _tagging("2" * 64, -1.0, 200))
        rows = (await db.execute(select(NostrUserTagging))).scalars().all()
        return [(r.polarity, r.created_at_unix) for r in rows]

    # The dispute REPLACES the apply. Getting this wrong double-counts every
    # re-assertion and inflates both sides of the membership predicate.
    assert run_with_db(work) == [(-1.0, 200)]


def test_tagging_older_flip_is_ignored():
    async def work(db):
        await upsert_user_tagging_on_db(db, _tagging("2" * 64, -1.0, 200))
        await upsert_user_tagging_on_db(db, _tagging("1" * 64, 1.0, 100))
        rows = (await db.execute(select(NostrUserTagging))).scalars().all()
        return [(r.polarity, r.created_at_unix) for r in rows]

    assert run_with_db(work) == [(-1.0, 200)]


def test_distinct_d_tags_from_same_asserter_coexist():
    async def work(db):
        await upsert_user_tagging_on_db(db, _tagging("1" * 64, 1.0, 100, d_tag="a"))
        await upsert_user_tagging_on_db(db, _tagging("2" * 64, 1.0, 100, d_tag="b"))
        rows = (await db.execute(select(NostrUserTagging))).scalars().all()
        return len(rows)

    assert run_with_db(work) == 2
