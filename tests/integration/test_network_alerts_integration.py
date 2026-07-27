"""Integration tests for GET /networkAlerts against a real Neo4j.

Requires Neo4j reachable at ``settings.neo4j_db_url``. Run explicitly with::

    poetry run pytest tests/integration/test_network_alerts_integration.py -m integration

These cover what the mocked suite structurally cannot: that the Cypher parses,
that REPORTS anchoring and live-edge section membership behave as intended, and
that the capped follower count truncates where it should.

Fixture graph — all per-observer properties are set from alice's perspective
(``influence_<alice>``, ``trusted_followers_<alice>``, ...). ``tf`` is the
stored verified-follower count, and ``None`` means the property is absent, which
is what forces the counted fallback. ``N`` is ``2 + floor(tf / 500)``; a row
alerts when its verified reporter count is strictly greater than N.

    name                  follows?  influence  tf     tr   N   alerts?
    direct_flagged        yes       0.001      0      5    2   direct
    direct_boundary       yes       0.400      0      2    2   no  (2 > 2 false)
    direct_just_over      yes       0.400      0      3    2   direct
    ext_flagged           no        0.500      0      4    2   extended
    ext_below_cutoff      no        0.001      0      9    2   no  (under cutoff)
    ext_scaled_blocked    no        0.500      1000   4    4   no  (4 > 4 false)
    ext_scaled_flagged    no        0.500      1000   5    4   extended
    both_sections         yes       0.900      0      6    2   direct ONLY
    stale_no_report_edge  yes       0.400      0      7    2   no  (no REPORTS edge)
    fallback_counted      yes       0.400      None   3    2   direct, tf counted
    alice (self)          --        0.990      0      9    2   no  (self-excluded)

``direct_flagged`` sits *below* the cutoff on purpose: the direct section is
defined by the follow edge alone, with no influence requirement.
``stale_no_report_edge`` carries a reporter count with no surviving REPORTS
edge — the shape left behind when reports are retracted (kind 5) before the
next GrapeRank run rewrites the property. ``fallback_counted`` is seeded with
three above-cutoff followers and one below, so the counted value is a known 3.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

from app.api import app
from app.core.config import settings
from app.repos.user_repo import count_above_cutoff_followers_capped

pytestmark = pytest.mark.integration

# influence, stored trusted_followers (None = absent), trusted_reporters,
# alice-follows, has-report-edge
_FIXTURE = {
    "direct_flagged": (0.001, 0, 5, True, True),
    "direct_boundary": (0.400, 0, 2, True, True),
    "direct_just_over": (0.400, 0, 3, True, True),
    "ext_flagged": (0.500, 0, 4, False, True),
    "ext_below_cutoff": (0.001, 0, 9, False, True),
    "ext_scaled_blocked": (0.500, 1000, 4, False, True),
    "ext_scaled_flagged": (0.500, 1000, 5, False, True),
    "both_sections": (0.900, 0, 6, True, True),
    "stale_no_report_edge": (0.400, 0, 7, True, False),
    "fallback_counted": (0.400, None, 3, True, True),
}

# Followers seeded onto `fallback_counted`: three above the 0.02 cutoff, one
# below. The counted verified-follower count must therefore come back as 3.
_FALLBACK_FOLLOWERS = [("f_hi1", 0.30), ("f_hi2", 0.20), ("f_hi3", 0.10)]
_FALLBACK_WEAK_FOLLOWERS = [("f_lo1", 0.001)]


def _fresh_driver():
    return AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )


@pytest.fixture(scope="module")
def graph():
    """Seed the fixture graph; yield {name: hex_pubkey}; clean up after."""
    follower_names = [n for n, _ in _FALLBACK_FOLLOWERS + _FALLBACK_WEAK_FOLLOWERS]
    names = (
        list(_FIXTURE)
        + follower_names
        + ["alice", "reporter", "muter_verified", "muter_weak"]
    )
    pks = {name: Keys.generate().public_key().to_hex() for name in names}
    alice = pks["alice"]

    inf_key = f"influence_{alice}"
    hops_key = f"hops_{alice}"
    tf_key = f"trusted_followers_{alice}"
    tr_key = f"trusted_reporters_{alice}"

    async def _seed():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                for name, (inf, tf, tr, followed, reported) in _FIXTURE.items():
                    # tf is set only when not None — absent means "no GrapeRank
                    # run has written trusted_followers for this observer yet".
                    tf_clause = f", n.`{tf_key}` = $tf" if tf is not None else ""
                    await s.run(
                        f"""
                        MERGE (n:NostrUser {{pubkey: $pk}})
                        SET n.`{inf_key}` = $inf,
                            n.`{hops_key}` = 2,
                            n.`{tr_key}` = $tr
                            {tf_clause}
                        """,
                        pk=pks[name],
                        inf=inf,
                        tf=tf,
                        tr=tr,
                    )
                    if followed:
                        await s.run(
                            """
                            MERGE (a:NostrUser {pubkey: $alice})
                            MERGE (b:NostrUser {pubkey: $pk})
                            MERGE (a)-[:FOLLOWS]->(b)
                            """,
                            alice=alice,
                            pk=pks[name],
                        )
                    if reported:
                        await s.run(
                            """
                            MERGE (r:NostrUser {pubkey: $reporter})
                            MERGE (b:NostrUser {pubkey: $pk})
                            MERGE (r)-[:REPORTS]->(b)
                            """,
                            reporter=pks["reporter"],
                            pk=pks[name],
                        )

                # Followers of `fallback_counted`, straddling the cutoff.
                for fname, finf in _FALLBACK_FOLLOWERS + _FALLBACK_WEAK_FOLLOWERS:
                    await s.run(
                        f"""
                        MERGE (f:NostrUser {{pubkey: $fpk}})
                        SET f.`{inf_key}` = $finf
                        MERGE (b:NostrUser {{pubkey: $target}})
                        MERGE (f)-[:FOLLOWS]->(b)
                        """,
                        fpk=pks[fname],
                        finf=finf,
                        target=pks["fallback_counted"],
                    )

                # Alice would clear the bar herself if she weren't excluded.
                await s.run(
                    f"""
                    MERGE (a:NostrUser {{pubkey: $alice}})
                    SET a.`{inf_key}` = 0.99, a.`{tf_key}` = 0, a.`{tr_key}` = 9
                    MERGE (r:NostrUser {{pubkey: $reporter}})
                    MERGE (r)-[:REPORTS]->(a)
                    """,
                    alice=alice,
                    reporter=pks["reporter"],
                )

                # Two muters on direct_flagged: only the above-cutoff one counts.
                await s.run(
                    f"""
                    MERGE (mv:NostrUser {{pubkey: $mv}}) SET mv.`{inf_key}` = 0.60
                    MERGE (mw:NostrUser {{pubkey: $mw}}) SET mw.`{inf_key}` = 0.001
                    MERGE (b:NostrUser {{pubkey: $target}})
                    MERGE (mv)-[:MUTES]->(b)
                    MERGE (mw)-[:MUTES]->(b)
                    """,
                    mv=pks["muter_verified"],
                    mw=pks["muter_weak"],
                    target=pks["direct_flagged"],
                )
        finally:
            await driver.close()

    async def _cleanup():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                await s.run(
                    "MATCH (n:NostrUser) WHERE n.pubkey IN $pks DETACH DELETE n",
                    pks=list(pks.values()),
                )
        finally:
            await driver.close()

    asyncio.run(_seed())
    yield pks
    asyncio.run(_cleanup())


@asynccontextmanager
async def _async_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _fetch(observer: str, **params) -> dict:
    async def _go():
        async with _async_client() as client:
            resp = await client.get(
                "/networkAlerts", params={"observer": observer, **params}
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["data"]

    return asyncio.run(_go())


def _names(data: dict, section: str, graph: dict) -> set[str]:
    by_pubkey = {v: k for k, v in graph.items()}
    return {by_pubkey[item["pubkey"]] for item in data[section]}


def _row(data: dict, graph: dict, name: str) -> dict:
    everything = data["directFollows"] + data["extendedNetwork"]
    return next(i for i in everything if i["pubkey"] == graph[name])


def test_direct_section_membership(graph):
    data = _fetch(graph["alice"])

    assert _names(data, "directFollows", graph) == {
        "direct_flagged",
        "direct_just_over",
        "both_sections",
        "fallback_counted",
    }


def test_extended_section_membership(graph):
    data = _fetch(graph["alice"])

    assert _names(data, "extendedNetwork", graph) == {
        "ext_flagged",
        "ext_scaled_flagged",
    }


def test_sections_are_disjoint(graph):
    """A pubkey qualifying for both is reported under direct follows only."""
    data = _fetch(graph["alice"])

    direct = _names(data, "directFollows", graph)
    extended = _names(data, "extendedNetwork", graph)
    assert "both_sections" in direct
    assert not direct & extended


def test_threshold_scales_with_verified_followers(graph):
    """1000 verified followers lifts N from 2 to 4, so 4 reports no longer alert."""
    data = _fetch(graph["alice"])

    everything = {i["pubkey"] for i in data["directFollows"] + data["extendedNetwork"]}
    assert graph["ext_scaled_blocked"] not in everything

    flagged = _row(data, graph, "ext_scaled_flagged")
    assert flagged["reporterThreshold"] == 4
    assert flagged["verifiedReporterCount"] == 5
    assert flagged["verifiedFollowerCount"] == 1000


def test_report_count_must_strictly_exceed_threshold(graph):
    """direct_boundary has exactly N=2 reports, so it is not an alert."""
    data = _fetch(graph["alice"])

    assert "direct_boundary" not in _names(data, "directFollows", graph)
    assert "direct_just_over" in _names(data, "directFollows", graph)


def test_direct_section_ignores_influence(graph):
    """A directly-followed pubkey alerts even below the trust cutoff."""
    data = _fetch(graph["alice"])

    assert _row(data, graph, "direct_flagged")["influence"] < 0.02


def test_below_cutoff_and_unfollowed_is_not_an_alert(graph):
    data = _fetch(graph["alice"])

    all_names = _names(data, "directFollows", graph) | _names(
        data, "extendedNetwork", graph
    )
    assert "ext_below_cutoff" not in all_names


def test_stale_reporter_count_without_report_edge_is_ignored(graph):
    """Anchoring on REPORTS means a retracted report stops alerting immediately,
    without waiting for the next GrapeRank run to rewrite the property."""
    data = _fetch(graph["alice"])

    assert "stale_no_report_edge" not in _names(data, "directFollows", graph)


def test_observer_is_never_alerted_about_themselves(graph):
    data = _fetch(graph["alice"])

    pubkeys = {i["pubkey"] for i in data["directFollows"] + data["extendedNetwork"]}
    assert graph["alice"] not in pubkeys


def test_verified_muter_count_excludes_below_cutoff_muters(graph):
    """direct_flagged has 2 muters; only the above-cutoff one is counted."""
    data = _fetch(graph["alice"])

    assert _row(data, graph, "direct_flagged")["verifiedMuterCount"] == 1


# ---------------------------------------------------------------------------
# Counted-fallback path (no stored trusted_followers_<observer>)
# ---------------------------------------------------------------------------
def test_missing_stored_count_is_counted_from_the_graph(graph):
    """fallback_counted has no stored property; 3 of its 4 followers clear the
    cutoff, so the endpoint must report exactly 3 — not 0, and not 4."""
    data = _fetch(graph["alice"])

    row = _row(data, graph, "fallback_counted")
    assert row["verifiedFollowerCount"] == 3
    assert row["reporterThreshold"] == 2


def test_capped_count_stops_at_the_cap(graph):
    """The LIMIT is what bounds a scan on a huge account — verify it truncates.

    Below the cap the count is exact; at the cap it stops, which is the signal
    the service uses to drop a row rather than publish a truncated number.
    """

    async def _go():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                inf_key = f"influence_{graph['alice']}"
                exact = await count_above_cutoff_followers_capped(
                    session=s,
                    pubkeys=[graph["fallback_counted"]],
                    influence_key=inf_key,
                    cutoff=0.02,
                    cap=10,
                )
                truncated = await count_above_cutoff_followers_capped(
                    session=s,
                    pubkeys=[graph["fallback_counted"]],
                    influence_key=inf_key,
                    cutoff=0.02,
                    cap=2,
                )
                return exact, truncated
        finally:
            await driver.close()

    exact, truncated = asyncio.run(_go())

    assert exact[graph["fallback_counted"]] == 3
    assert truncated[graph["fallback_counted"]] == 2


def test_capped_count_rejects_a_nonsense_cap(graph):
    """`cap` is interpolated into the Cypher, so it's guarded at that site."""

    async def _go():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                await count_above_cutoff_followers_capped(
                    session=s,
                    pubkeys=[graph["fallback_counted"]],
                    influence_key=f"influence_{graph['alice']}",
                    cutoff=0.02,
                    cap="1 RETURN 1 //",
                )
        finally:
            await driver.close()

    with pytest.raises(ValueError):
        asyncio.run(_go())


def test_limit_caps_each_section_and_flags_truncation(graph):
    data = _fetch(graph["alice"], limit=1)

    assert len(data["directFollows"]) == 1
    assert len(data["extendedNetwork"]) == 1
    assert data["directFollowsTruncated"] is True
    assert data["extendedNetworkTruncated"] is True


def test_unknown_observer_returns_empty_sections(graph):
    """A pubkey absent from the graph has no perspective — not an error."""
    data = _fetch(Keys.generate().public_key().to_hex())

    assert data["directFollows"] == []
    assert data["extendedNetwork"] == []
