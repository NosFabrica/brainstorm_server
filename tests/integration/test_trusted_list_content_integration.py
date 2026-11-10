"""The content dictionary over real Postgres and Neo4j (story 2, AC9; ADR
`trusted-lists/0002` D6, D7): which taggings a read returns, and that the
pairs depend on each Observer's own web of trust.

Targets here are listings named by coordinate, whose tagging target (kind
30402) needs no lookup, so no relay is involved.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from neo4j import AsyncGraphDatabase

from app.core.config import settings
from app.repos.event_tagging_repo import (
    get_event_taggings_on_db,
    upsert_event_tagging_on_db,
    upsert_tagging_header_on_db,
)
from app.repos.tagging_repo import upsert_tag_element_on_db
from app.repos.user_repo import get_qualifying_asserters_for_observer
from app.services.tagging_parse import EventTagging, TagElement, TaggingHeader
from app.services.trusted_list_service import TargetView, plan_content_lists
from tests.integration.tagging_harness import run_with_db, run_with_db_and_graph

pytestmark = pytest.mark.integration

OBSERVER_X = "1" * 64
OBSERVER_Y = "2" * 64
AUTHOR = "3" * 64
HEAVY = "4" * 64
LIGHT = "5" * 64
LISTING = f"30402:{'6' * 64}:bike"


def _element(slug="espresso", event_id="7" * 64, created_at=100):
    return TagElement(
        event_id=event_id,
        author_pubkey=AUTHOR,
        slug=slug,
        name=slug,
        description="",
        created_at_unix=created_at,
    )


def _header(slug="espresso", created_at=100):
    return TaggingHeader(
        event_id="8" * 64 if created_at == 100 else "9" * 64,
        author_pubkey=AUTHOR,
        d_tag="tagging:espresso-tagging",
        tag_author_pubkey=AUTHOR,
        tag_slug=slug,
        tag_event_id=None,
        created_at_unix=created_at,
    )


def _tagging(asserter, target=LISTING):
    return EventTagging(
        event_id=asserter[:32] + "a" * 32,
        asserter_pubkey=asserter,
        d_tag=f"event-tag-{asserter[:6]}",
        target=target,
        target_slot="a",
        target_relay=None,
        header_coordinate=f"39999:{AUTHOR}:tagging:espresso-tagging",
        polarity=1.0,
        created_at_unix=100,
    )


def _plan(observer, rows, weights, min_uses=1):
    return plan_content_lists(
        observer=observer,
        rows=rows,
        view=TargetView(),
        weights=weights,
        cutoff=1,
        rigor=0.5,
        min_uses=min_uses,
    )


def test_two_observers_get_different_content_dictionaries_from_same_taggings():
    async def work(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_tagging_header_on_db(db, _header())
        await upsert_event_tagging_on_db(db, _tagging(HEAVY))
        await upsert_event_tagging_on_db(db, _tagging(LIGHT))
        await db.flush()
        neo = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        out = {}
        try:
            async with neo.session() as s:
                # Y's web of trust holds HEAVY only.
                await s.run(
                    "MATCH (u:NostrUser {pubkey: $pk}) SET u[$key] = 0.9",
                    pk=HEAVY,
                    key=f"influence_{OBSERVER_Y}",
                )
                for observer in (OBSERVER_X, OBSERVER_Y):
                    weights = await get_qualifying_asserters_for_observer(
                        s,
                        pubkeys=[HEAVY, LIGHT],
                        observer_pubkey=observer,
                        min_influence=settings.trusted_list_min_rank / 100.0,
                    )
                    rows = await get_event_taggings_on_db(db, list(weights))
                    out[observer] = (
                        sorted(weights),
                        _plan(observer, rows, weights, min_uses=1),
                        _plan(observer, rows, weights, min_uses=2),
                    )
        finally:
            await neo.close()
        return out

    # X trusts both asserters; Y trusts only HEAVY.
    nodes = {HEAVY: 0.9, LIGHT: 0.9}
    result = run_with_db_and_graph(work, nodes, OBSERVER_X)
    x_weights, x_one, x_two = result[OBSERVER_X]
    y_weights, y_one, y_two = result[OBSERVER_Y]
    assert x_weights == [HEAVY, LIGHT]
    assert y_weights == [HEAVY]
    assert [(lst.title, lst.members[0].applications) for lst in x_one.lists] == [
        ("espresso Listings", 2)
    ]
    assert [(lst.title, lst.members[0].applications) for lst in y_one.lists] == [
        ("espresso Listings", 1)
    ]
    # min_uses = 2: X's two uses meet it, Y's one use doesn't.
    assert len(x_two.lists) == 1
    assert y_two.lists == []


def test_tagging_with_missing_header_or_tag_joins_once_both_arrive():
    async def work(db):
        await upsert_event_tagging_on_db(db, _tagging(HEAVY))
        await db.flush()
        no_header = await get_event_taggings_on_db(db, [HEAVY])
        await upsert_tagging_header_on_db(db, _header())
        await db.flush()
        no_element = await get_event_taggings_on_db(db, [HEAVY])
        await upsert_tag_element_on_db(db, _element())
        await db.flush()
        joined = await get_event_taggings_on_db(db, [HEAVY])
        return no_header, no_element, joined

    no_header, no_element, joined = run_with_db(work)
    assert no_header == [] and no_element == []
    assert [(r.target, r.slug, r.tag_author_pubkey) for r in joined] == [
        (LISTING, "espresso", AUTHOR)
    ]


def test_repointed_header_moves_its_taggings_next_run():
    async def work(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_tag_element_on_db(db, _element("coffee", event_id="b" * 64))
        await upsert_tagging_header_on_db(db, _header())
        await upsert_event_tagging_on_db(db, _tagging(HEAVY))
        await db.flush()
        before = await get_event_taggings_on_db(db, [HEAVY])
        await upsert_tagging_header_on_db(db, replace(_header("coffee", created_at=200)))
        await db.flush()
        after = await get_event_taggings_on_db(db, [HEAVY])
        return before, after

    before, after = run_with_db(work)
    assert [r.slug for r in before] == ["espresso"]
    assert [r.slug for r in after] == ["coffee"]
