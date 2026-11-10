"""Storage semantics for tagging headers and event taggings (story 2, AC1, AC2,
AC17b), against real Postgres. ADR `trusted-lists/0002` D6, D10."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.db_models import NostrEventTagging, NostrTaggingHeader
from app.message_queue_tasks import process_strfry_event as ingest
from app.repos.event_tagging_repo import (
    upsert_event_tagging_on_db,
    upsert_tagging_header_on_db,
)
from app.services.tagging_parse import EventTagging, TaggingHeader
from scripts.backfill_event_taggings import replay
from tests.integration.tagging_harness import run_with_db
from tests.test_event_tagging_ingest import _header, _tagging

pytestmark = pytest.mark.integration

AUTHOR = "2" * 64
ASSERTER = "b" * 64

HEADER = TaggingHeader(
    event_id="e" * 64,
    author_pubkey=AUTHOR,
    d_tag="tagging:espresso-tagging",
    tag_author_pubkey=AUTHOR,
    tag_slug="espresso",
    tag_event_id=None,
    created_at_unix=100,
)
TAGGING = EventTagging(
    event_id="f" * 64,
    asserter_pubkey=ASSERTER,
    d_tag="event-tag-espresso-x",
    target="1" * 64,
    target_slot="e",
    target_relay=None,
    header_coordinate=f"39999:{AUTHOR}:tagging:espresso-tagging",
    polarity=1.0,
    created_at_unix=100,
)


async def _headers(db):
    return (await db.execute(select(NostrTaggingHeader))).scalars().all()


async def _taggings(db):
    return (await db.execute(select(NostrEventTagging))).scalars().all()


def test_tagging_header_upsert_newer_replaces_older_ignored():
    async def work(db):
        await upsert_tagging_header_on_db(db, HEADER)
        await upsert_tagging_header_on_db(
            db, replace(HEADER, event_id="9" * 64, tag_slug="coffee", created_at_unix=200)
        )
        await upsert_tagging_header_on_db(
            db, replace(HEADER, event_id="8" * 64, tag_slug="tea", created_at_unix=150)
        )
        await db.flush()
        return [(h.event_id, h.tag_slug) for h in await _headers(db)]

    assert run_with_db(work) == [("9" * 64, "coffee")]


def test_event_tagging_upsert_latest_wins_across_polarity_flip():
    async def work(db):
        await upsert_event_tagging_on_db(db, TAGGING)
        await upsert_event_tagging_on_db(
            db, replace(TAGGING, event_id="7" * 64, polarity=-1.0, created_at_unix=200)
        )
        await db.flush()
        return [(t.event_id, t.polarity) for t in await _taggings(db)]

    assert run_with_db(work) == [("7" * 64, -1.0)]


def test_event_tagging_older_event_does_not_overwrite():
    async def work(db):
        await upsert_event_tagging_on_db(db, replace(TAGGING, created_at_unix=200))
        await upsert_event_tagging_on_db(
            db, replace(TAGGING, event_id="7" * 64, polarity=-1.0, created_at_unix=150)
        )
        await db.flush()
        rows = await _taggings(db)
        return [(t.event_id, t.polarity, t.target, t.target_slot, t.header_coordinate) for t in rows]

    assert run_with_db(work) == [
        ("f" * 64, 1.0, "1" * 64, "e", f"39999:{AUTHOR}:tagging:espresso-tagging")
    ]


def test_backfill_apply_twice_changes_nothing_the_second_time(monkeypatch):
    """The live handler, on this test's session: run 1 writes, run 2 is a no-op."""

    async def work(db):
        @asynccontextmanager
        async def this_session():
            yield db
            await db.flush()

        monkeypatch.setattr(ingest, "db_session", this_session)
        events = [_header(), _tagging(), _tagging(id="9" * 64, created_at=1)]

        async def snapshot():
            headers = [(h.author_pubkey, h.d_tag, h.event_id) for h in await _headers(db)]
            taggings = [
                (t.asserter_pubkey, t.d_tag, t.event_id, t.created_at_unix)
                for t in await _taggings(db)
            ]
            return headers, taggings

        first = await replay(events, apply=True)
        after_first = await snapshot()
        second = await replay(events, apply=True)
        after_second = await snapshot()
        return first, after_first, second, after_second

    first, after_first, second, after_second = run_with_db(work)
    assert first["headers"] == 1 and first["event_taggings"] == 2
    assert first.get("failed", 0) == 0 and second.get("failed", 0) == 0
    # The older tagging at the same (asserter, d) never replaced the newer.
    assert [t[2] for t in after_first[1]] == ["f" * 64]
    assert after_second == after_first
