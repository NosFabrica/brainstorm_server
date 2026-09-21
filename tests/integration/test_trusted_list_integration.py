"""Dictionary computation under a real Observer's web of trust (AC5, AC6),
plus the dangling-reference drop (S2).

These need both backends: the taggings live in Postgres, the rank that decides
whose taggings count lives in Neo4j as per-observer node properties.
"""
from __future__ import annotations

import pytest
from neo4j import AsyncGraphDatabase

from app.core.config import settings
from app.repos.tagging_repo import (
    get_dictionary_on_db,
    get_taggings_for_tag_on_db,
    upsert_tag_element_on_db,
    upsert_user_tagging_on_db,
)
from app.repos.user_repo import get_qualifying_asserters_for_observer
from app.services.tagging_parse import TagElement, UserTagging
from tests.integration.tagging_harness import run_with_db_and_graph

pytestmark = pytest.mark.integration

OBSERVER_A = "a" * 64
OBSERVER_B = "b" * 64
HIGH = "1" * 64  # well above threshold
EXACT = "2" * 64  # exactly at threshold
LOW = "3" * 64  # below threshold
UNSCORED = "4" * 64  # no influence property at all
TARGET = "9" * 64
TAG_AUTHOR = "8" * 64
TAG_EV = "7" * 64
OTHER_TAG_EV = "6" * 64

# TRUSTED_LIST_MIN_RANK default is 3 -> influence floor 0.03.
MIN_INFLUENCE = 0.03


def _element(event_id=TAG_EV, slug="podcaster", name="Podcaster"):
    return TagElement(
        event_id=event_id,
        author_pubkey=TAG_AUTHOR,
        slug=slug,
        name=name,
        description="Makes podcasts",
        created_at_unix=100,
    )


def _tagging(asserter, tag_event_id=TAG_EV, polarity=1.0, target=TARGET, d="d"):
    return UserTagging(
        event_id=asserter[:32] + tag_event_id[:32],
        asserter_pubkey=asserter,
        d_tag=f"{d}-{asserter[:4]}",
        target_pubkey=target,
        tag_event_id=tag_event_id,
        polarity=polarity,
        created_at_unix=100,
    )


async def _qualifying(observer, pubkeys):
    neo = AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )
    try:
        async with neo.session() as s:
            return await get_qualifying_asserters_for_observer(
                s,
                pubkeys=pubkeys,
                observer_pubkey=observer,
                min_influence=MIN_INFLUENCE,
            )
    finally:
        await neo.close()


# --- D12: the asserter weight the fold prices assertions with ---------------


def test_qualifying_asserters_carry_rank_quantized_weights():
    """The repo returns `{pubkey: weight}`, and the weight is Influence on the
    Rank quantum — `round(influence * 100) / 100`. Tapestry derives the same
    number as `wot_rank / 100`, so any drift here silently forks the two
    estates' scores while both still look plausible.
    """
    nodes = {HIGH: 0.9, EXACT: MIN_INFLUENCE, LOW: 0.01}

    async def work(db):
        return await _qualifying(OBSERVER_A, [HIGH, EXACT, LOW])

    qual = run_with_db_and_graph(work, nodes, OBSERVER_A)
    assert qual[HIGH] == 0.9
    assert LOW not in qual
    # Every weight is a whole number of Rank points, never a raw influence
    # float. Compared with a tolerance because `n / 100 * 100` is not exact in
    # binary floating point for every n.
    assert all(abs(w * 100 - round(w * 100)) < 1e-9 for w in qual.values())


# --- AC5 / AC6 -------------------------------------------------------------


def test_dictionary_contains_only_tags_used_by_qualifying_asserters():
    nodes = {HIGH: 0.9, EXACT: MIN_INFLUENCE, LOW: 0.01, UNSCORED: None}

    async def work(db):
        await upsert_tag_element_on_db(db, _element())
        for a in (HIGH, EXACT, LOW, UNSCORED):
            await upsert_user_tagging_on_db(db, _tagging(a))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [HIGH, EXACT, LOW, UNSCORED])
        entries = await get_dictionary_on_db(db, list(qual), min_uses=1)
        return sorted(qual), entries

    qual, entries = run_with_db_and_graph(work, nodes, OBSERVER_A)

    # EXACT is the boundary: the floor is INCLUSIVE (ADR D4), so it qualifies.
    assert sorted(qual) == sorted([HIGH, EXACT])
    assert LOW not in qual
    # "Never scored" is not "scored zero" — UNSCORED is simply absent.
    assert UNSCORED not in qual
    assert len(entries) == 1
    assert entries[0].slug == "podcaster"
    assert entries[0].name == "Podcaster"
    assert entries[0].description == "Makes podcasts"
    assert entries[0].uses == 2  # HIGH + EXACT only


def test_dictionary_excludes_subthreshold_only_and_unreferenced_tags():
    nodes = {LOW: 0.01}

    async def work(db):
        # An element nobody qualified has used, and one used only by LOW.
        await upsert_tag_element_on_db(db, _element())
        await upsert_tag_element_on_db(
            db, _element(event_id=OTHER_TAG_EV, slug="chef", name="Chef")
        )
        await upsert_user_tagging_on_db(db, _tagging(LOW))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [LOW])
        return qual, await get_dictionary_on_db(db, list(qual), min_uses=1)

    qual, entries = run_with_db_and_graph(work, nodes, OBSERVER_A)
    assert qual == {}
    # No qualifying asserters -> empty dictionary, and the never-used tag is
    # absent regardless.
    assert entries == []


def test_two_observers_get_different_dictionaries_from_same_taggings():
    """AC6: the same tagging data, two Observers, two answers."""
    # HIGH is trusted by A but not by B.
    nodes = {HIGH: 0.9}

    async def work_a(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_user_tagging_on_db(db, _tagging(HIGH))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [HIGH])
        return await get_dictionary_on_db(db, list(qual), min_uses=1)

    entries_a = run_with_db_and_graph(work_a, nodes, OBSERVER_A)
    assert len(entries_a) == 1

    async def work_b(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_user_tagging_on_db(db, _tagging(HIGH))
        await db.flush()
        # Same pubkey, but scored under OBSERVER_B's key at 0.001.
        qual = await _qualifying(OBSERVER_B, [HIGH])
        return await get_dictionary_on_db(db, list(qual), min_uses=1)

    entries_b = run_with_db_and_graph(work_b, {HIGH: 0.001}, OBSERVER_B)
    assert entries_b == []


def test_min_uses_threshold_is_honoured():
    nodes = {HIGH: 0.9, EXACT: 0.5}

    async def work(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_user_tagging_on_db(db, _tagging(HIGH))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [HIGH, EXACT])
        one = await get_dictionary_on_db(db, list(qual), min_uses=1)
        two = await get_dictionary_on_db(db, list(qual), min_uses=2)
        return len(one), len(two)

    # Issue #73 anticipates raising the threshold; it must actually bite.
    assert run_with_db_and_graph(work, nodes, OBSERVER_A) == (1, 0)


# --- S2: dangling tag reference -------------------------------------------


def test_tagging_referencing_unknown_tag_element_is_dropped():
    nodes = {HIGH: 0.9}

    async def work(db):
        # No element row for TAG_EV — the tagging references a tag we never got.
        await upsert_user_tagging_on_db(db, _tagging(HIGH))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [HIGH])
        return await get_dictionary_on_db(db, list(qual), min_uses=1)

    # Must be dropped, not surfaced as a TL with an empty title.
    assert run_with_db_and_graph(work, nodes, OBSERVER_A) == []


# --- neutral polarity ------------------------------------------------------


def test_neutral_polarity_taggings_do_not_create_dictionary_entries():
    nodes = {HIGH: 0.9}

    async def work(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_user_tagging_on_db(db, _tagging(HIGH, polarity=0.0))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [HIGH])
        return await get_dictionary_on_db(db, list(qual), min_uses=1)

    # The reserved open interval counts as neither use.
    assert run_with_db_and_graph(work, nodes, OBSERVER_A) == []


def test_taggings_for_tag_restricted_to_qualifying_asserters():
    nodes = {HIGH: 0.9, LOW: 0.001}

    async def work(db):
        await upsert_tag_element_on_db(db, _element())
        await upsert_user_tagging_on_db(db, _tagging(HIGH))
        await upsert_user_tagging_on_db(db, _tagging(LOW))
        await db.flush()
        qual = await _qualifying(OBSERVER_A, [HIGH, LOW])
        return await get_taggings_for_tag_on_db(db, TAG_EV, list(qual))

    rows = run_with_db_and_graph(work, nodes, OBSERVER_A)
    # LOW's assertion must not reach the membership computation at all. The
    # asserter rides along so the weighted fold can price the assertion.
    assert rows == [(TARGET, 1.0, HIGH)]
