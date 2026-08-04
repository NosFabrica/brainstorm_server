"""The Python tier classifier and the Cypher tier table agree, subject for subject.

Tier bucketing exists twice: `_TIER_PREDICATES` in Cypher, for the read
endpoints that bucket subjects in the graph, and `classify_tier` in Python, for
the GrapeRank result writer, which buckets scorecards into
`BrainstormRequest.count_values` before a separate consumer has written them to
the graph — so it has no query to run. Neither can call the other, hence this:
every fixture subject's `tier` from `/connections` must equal what
`classify_tier` says about the same influence and reporter count.

Requires the local stack (Neo4j at ``settings.neo4j_db_url``).

Issue: .scratch/preset-verified-counts/issues/04-frontend-render-backend-truth.md
"""

import asyncio

import pytest

from app.core.tier_thresholds import TIER_NAMES, classify_tier
from tests.integration.preset_graph import (
    DEFAULT_CUTOFFS,
    RESTRICTIVE_CUTOFFS,
    api,
    fetch_connections,
    seed_graph,
)

pytestmark = pytest.mark.integration

# One subject per interesting position: inside each band, exactly on a band
# edge, exactly on each preset's line (the strict-`>` case), below the line with
# and without 2 trusted reporters, and no influence property at all.
_NODES: dict[str, tuple[float | None, int]] = {
    "subject": (0.4, 0),
    "n_top": (0.95, 0),
    "n_on_high_band": (0.5, 0),
    "n_high_mid": (0.6, 3),
    "n_on_medium_high_band": (0.2, 0),
    "n_medium": (0.1, 0),
    "n_on_medium_band": (0.07, 2),
    "n_medium_low": (0.03, 0),
    "n_on_default_line": (0.02, 0),
    "n_on_default_line_reported": (0.02, 2),
    "n_below": (0.005, 0),
    "n_below_reported": (0.005, 4),
    "n_zero": (0.0, 0),
    "n_none": (None, 0),
    "n_none_reported": (None, 2),
}

_EDGES = [(name, "FOLLOWS", "subject") for name in _NODES if name != "subject"]


@pytest.fixture(scope="module")
def graph():
    yield from seed_graph(_NODES, _EDGES)


def test_python_classifier_matches_the_cypher_tier_for_every_subject(graph):
    async def body():
        by_pubkey = {graph[name]: _NODES[name] for name in _NODES if name != "subject"}

        for cutoffs in (DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS):
            async with api(cutoffs) as client:
                page = await fetch_connections(
                    client, graph["subject"], "followed_by", limit=200
                )
                assert len(page["items"]) == len(by_pubkey)

                for item in page["items"]:
                    influence, trusted_reporters = by_pubkey[item["pubkey"]]
                    expected = classify_tier(
                        influence, trusted_reporters, cutoffs.verified_line
                    )
                    assert item["tier"] == expected, (
                        cutoffs.verified_line,
                        influence,
                        trusted_reporters,
                    )

    asyncio.run(body())


def test_the_two_implementations_use_the_same_bucket_names(graph):
    """A rename on one side has to fail here rather than quietly produce a
    `count_values` key no endpoint ever returns."""

    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            seen = set()
            for tier in TIER_NAMES:
                page = await fetch_connections(
                    client, graph["subject"], "followed_by", tier=tier, limit=200
                )
                seen.update(item["tier"] for item in page["items"])
            # Every name the classifier knows is a name the endpoint accepts and
            # echoes back, and the fixture covers enough of them to be meaningful.
            assert seen <= set(TIER_NAMES)
            assert len(seen) >= 4

    asyncio.run(body())
