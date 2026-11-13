"""Each row's Influence is read once: Neo4j walks a node's whole property list
for every `node[$key]`, so a query that mentions it per tier pays per mention."""

import asyncio

from app.repos import user_repo
from app.repos.user_repo import (
    _TIER_PREDICATES,
    _expand,
    _live_reporters,
    _row_map,
    _tier_case,
)

READ = "[$influence_key]"


class _Recorder:
    """Stands in for a Neo4j session: keeps the query, answers with no rows."""

    def __init__(self):
        self.queries: list[str] = []

    async def run(self, query, **_):
        self.queries.append(query)
        return self

    async def single(self):
        return None


def _without_reporters(cypher: str) -> str:
    return cypher.replace(_live_reporters(), "").replace(_live_reporters("user"), "")


def test_tier_fragments_use_the_bound_score():
    fragments = [
        *(_expand(predicate) for predicate in _TIER_PREDICATES.values()),
        _tier_case(),
        _tier_case("user"),
        _row_map(),
    ]

    assert all(READ not in _without_reporters(f) for f in fragments)


def test_stats_reads_each_followers_score_once_per_section():
    session = _Recorder()
    cutoffs = {name: 0.02 for name, _, _ in user_repo._STATS_KINDS}

    asyncio.run(
        user_repo.get_all_section_stats(
            session,
            "a" * 64,
            "influence_x",
            verified_cutoff_by_kind=cutoffs,
            verified_line=0.02,
            reporter_cutoff=0.1,
        )
    )

    (query,) = session.queries
    assert query.count("other" + READ) == len(user_repo._STATS_KINDS)


def test_a_connections_page_reads_each_score_once():
    session = _Recorder()

    asyncio.run(
        user_repo.get_paginated_section_connections(
            session,
            "a" * 64,
            "influence_x",
            "FOLLOWS",
            "in",
            50,
            None,
            None,
            verified_cutoff=0.02,
            verified_line=0.02,
            reporter_cutoff=0.1,
            tier="high",
            with_total=True,
        )
    )

    (query,) = session.queries
    # Once for the page, once for the filtered total.
    assert query.count("other" + READ) == 2


def test_no_score_is_aliased_as_a_float_literal():
    source = open(user_repo.__file__).read().lower()

    assert not any(
        f" as {name}\n" in source or f" as {name} " in source
        for name in ("inf", "infinity", "nan")
    )
