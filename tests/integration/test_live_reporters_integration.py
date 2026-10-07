"""Flagged is judged from the live REPORTS edges, not a count stored at the last run.

A verified reporter reports the subject and has Influence strictly above the
observer's current preset reporter cutoff. Every profile read must agree::

    poetry run pytest tests/integration/test_live_reporters_integration.py -m integration
"""

import asyncio

import pytest

from tests.integration.preset_graph import (
    DEFAULT_CUTOFFS,
    PERMISSIVE_CUTOFFS,
    RESTRICTIVE_CUTOFFS,
    api,
    fetch_connections,
    fetch_overview,
    fetch_stats,
    fresh_driver,
    seed_graph,
)

pytestmark = pytest.mark.integration

FLAGGED = "low_and_reported_by_2_or_more_trusted_pubkeys"

# Reporter cutoffs: PERMISSIVE 0.002, DEFAULT 0.1, RESTRICTIVE 0.5.
_NODES: dict[str, tuple[float | None, int]] = {
    "subject": (0.4, 0),
    "rep_hi_1": (0.9, 0),
    "rep_hi_2": (0.9, 0),
    "rep_at_default": (0.1, 0),  # exactly DEFAULT's reporter cutoff
    "rep_mid": (0.3, 0),  # clears DEFAULT, not RESTRICTIVE
    "two_above": (0.005, 0),
    "one_at_cutoff": (0.005, 0),
    "swing": (0.001, 0),  # at or below every preset's line
    "retracted": (0.005, 0),
    "verified_reported": (0.6, 0),
}

_FOLLOWERS = (
    "two_above",
    "one_at_cutoff",
    "swing",
    "retracted",
    "verified_reported",
)

_EDGES: list[tuple[str, str, str]] = [
    *[(n, "FOLLOWS", "subject") for n in _FOLLOWERS],
    ("rep_hi_1", "REPORTS", "two_above"),
    ("rep_hi_2", "REPORTS", "two_above"),
    ("rep_hi_1", "REPORTS", "one_at_cutoff"),
    ("rep_at_default", "REPORTS", "one_at_cutoff"),
    ("rep_hi_1", "REPORTS", "swing"),
    ("rep_mid", "REPORTS", "swing"),
    ("rep_hi_1", "REPORTS", "retracted"),
    ("rep_hi_2", "REPORTS", "retracted"),
    *[
        (r, "REPORTS", "verified_reported")
        for r in ("rep_hi_1", "rep_hi_2", "rep_mid", "rep_at_default")
    ],
]


@pytest.fixture(scope="module")
def graph():
    yield from seed_graph(_NODES, _EDGES)


async def _flagged(client, pubkey: str) -> bool:
    return (await fetch_overview(client, pubkey))["flagged_by_observer"]


async def _signal_flagged(client, pubkey: str) -> bool:
    resp = await client.post("/user/trustSignals", json={"pubkeys": [pubkey]})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["results"][0]["flagged"]


async def _rows(client, subject: str, kind: str, **params) -> dict[str, dict]:
    page = await fetch_connections(client, subject, kind, limit=200, **params)
    return {item["pubkey"]: item for item in page["items"]}


def test_two_reporters_above_the_reporter_cutoff_flag(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            assert await _flagged(client, graph["two_above"]) is True
            assert await _signal_flagged(client, graph["two_above"]) is True
            # Strict `>`: a reporter exactly on the cutoff doesn't count.
            assert await _flagged(client, graph["one_at_cutoff"]) is False
            assert await _signal_flagged(client, graph["one_at_cutoff"]) is False
            # Above the line, reports never flag.
            assert await _flagged(client, graph["verified_reported"]) is False

    asyncio.run(body())


async def _set_report(src: str, dst: str, present: bool) -> None:
    driver = fresh_driver()
    try:
        async with driver.session() as session:
            await session.run(
                "MATCH (a:NostrUser {pubkey: $src}), (b:NostrUser {pubkey: $dst}) "
                + (
                    "MERGE (a)-[:REPORTS]->(b)"
                    if present
                    else "MATCH (a)-[r:REPORTS]->(b) DELETE r"
                ),
                src=src,
                dst=dst,
            )
    finally:
        await driver.close()


def test_retracting_a_report_unflags_on_the_next_read(graph):
    async def body():
        subject, target = graph["subject"], graph["retracted"]
        async with api(DEFAULT_CUTOFFS) as client:
            assert await _flagged(client, target) is True
            before = await fetch_overview(client, subject)
            await _set_report(graph["rep_hi_2"], target, present=False)
            try:
                assert await _flagged(client, target) is False
                assert await _signal_flagged(client, target) is False
                after = await fetch_overview(client, subject)
                assert after["flagged_count"] == before["flagged_count"] - 1
                rows = await _rows(client, subject, "followed_by")
                assert rows[target]["trusted_reporters"] == 1
                assert rows[target]["tier"] == "low"
            finally:
                await _set_report(graph["rep_hi_2"], target, present=True)
            assert await _flagged(client, target) is True

    asyncio.run(body())


@pytest.mark.parametrize(
    "cutoffs, swing_flagged",
    [
        (PERMISSIVE_CUTOFFS, True),  # 0.3 > 0.002
        (DEFAULT_CUTOFFS, True),  # 0.3 > 0.1
        (RESTRICTIVE_CUTOFFS, False),  # 0.3 <= 0.5
    ],
)
def test_the_observers_preset_reporter_cutoff_decides(graph, cutoffs, swing_flagged):
    async def body():
        async with api(cutoffs) as client:
            assert await _flagged(client, graph["swing"]) is swing_flagged
            assert await _signal_flagged(client, graph["swing"]) is swing_flagged
            rows = await _rows(client, graph["subject"], "followed_by")
            assert rows[graph["swing"]]["trusted_reporters"] == (
                2 if swing_flagged else 1
            )
            flagged = await _rows(client, graph["subject"], "flagged")
            assert (graph["swing"] in flagged) is swing_flagged

    asyncio.run(body())


def test_row_trusted_reporters_is_the_live_verified_count(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            rows = await _rows(client, graph["subject"], "followed_by")
            flagged_rows = await _rows(client, graph["subject"], "flagged")
        counts = {name: rows[graph[name]]["trusted_reporters"] for name in _FOLLOWERS}
        assert counts == {
            "two_above": 2,
            "one_at_cutoff": 1,
            "swing": 2,
            "retracted": 2,
            "verified_reported": 3,  # rep_at_default sits on the cutoff
        }
        assert {pk: r["trusted_reporters"] for pk, r in flagged_rows.items()} == {
            graph["two_above"]: 2,
            graph["swing"]: 2,
            graph["retracted"]: 2,
        }

    asyncio.run(body())


def test_stats_and_tier_filter_count_live_flagged(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            stats = await fetch_stats(client, graph["subject"])
            tier_page = await fetch_connections(
                client, graph["subject"], "followed_by", tier=FLAGGED, with_total=True
            )
            flagged_page = await fetch_connections(
                client, graph["subject"], "flagged", with_total=True
            )
            overview = await fetch_overview(client, graph["subject"])
        expected = {graph[n] for n in ("two_above", "swing", "retracted")}
        assert stats["followed_by"]["tier_counts"][FLAGGED] == 3
        assert stats["followed_by"]["tier_counts"]["low"] == 1  # one_at_cutoff
        assert tier_page["total"] == 3
        assert {i["pubkey"] for i in tier_page["items"]} == expected
        assert flagged_page["total"] == 3
        assert {i["pubkey"] for i in flagged_page["items"]} == expected
        assert overview["flagged_count"] == 3

    asyncio.run(body())
