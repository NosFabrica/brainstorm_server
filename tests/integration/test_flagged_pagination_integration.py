"""Flagged lists page by (influence, pubkey) and report the full flagged total.

Covers kind=flagged and ?tier=flagged on followed_by::

    poetry run pytest tests/integration/test_flagged_pagination_integration.py -m integration
"""

import asyncio

import pytest

from tests.integration.preset_graph import (
    DEFAULT_CUTOFFS,
    api,
    fetch_connections,
    seed_graph,
)

pytestmark = pytest.mark.integration

FLAGGED = "low_and_reported_by_2_or_more_trusted_pubkeys"

# DEFAULT line 0.02; flagged = influence <= 0.02 with >= 2 verified reporters.
_NODES: dict[str, tuple[float | None, int]] = {
    "subject": (0.4, 0),
    "lonely": (0.4, 0),
    "f_015": (0.015, 2),
    "f_010_a": (0.01, 3),
    "f_010_b": (0.01, 2),
    "f_005": (0.005, 2),
    "f_001": (0.001, 2),
    "muter_flagged": (0.008, 2),  # mutes the subject, doesn't follow
    "one_report": (0.01, 1),
    "verified": (0.5, 2),
    "lonely_follower": (0.5, 0),
}

_EDGES: list[tuple[str, str, str]] = [
    *[
        (n, "FOLLOWS", "subject")
        for n in (
            "f_015",
            "f_010_a",
            "f_010_b",
            "f_005",
            "f_001",
            "one_report",
            "verified",
        )
    ],
    ("subject", "FOLLOWS", "f_005"),  # second edge must not duplicate the row
    ("muter_flagged", "MUTES", "subject"),
    ("lonely_follower", "FOLLOWS", "lonely"),
]


@pytest.fixture(scope="module")
def graph():
    yield from seed_graph(_NODES, _EDGES)


def _desc(graph, *, with_muter: bool) -> list[str]:
    tied = sorted([graph["f_010_a"], graph["f_010_b"]])
    head = [graph["f_015"], *tied]
    tail = [graph["f_005"], graph["f_001"]]
    return head + ([graph["muter_flagged"]] if with_muter else []) + tail


async def _walk(client, subject: str, kind: str, limit: int, **params):
    pages = []
    cursor = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = await fetch_connections(
            client, subject, kind, limit=limit, with_total=True, **params, **extra
        )
        pages.append(page)
        cursor = page["next_cursor"]
        if not cursor:
            return pages


_LISTS = [
    pytest.param("flagged", {}, True, id="kind=flagged"),
    pytest.param("followed_by", {"tier": FLAGGED}, False, id="tier=flagged"),
]


@pytest.mark.parametrize("kind, params, with_muter", _LISTS)
@pytest.mark.parametrize("order", ["desc", "asc"])
@pytest.mark.parametrize("limit", [2, 3])
def test_pages_walk_the_flagged_set_in_order(
    graph, kind, params, with_muter, order, limit
):
    expected = _desc(graph, with_muter=with_muter)
    if order == "asc":
        # Influence flips; ties stay pubkey-ascending.
        by_inf: dict[float, list[str]] = {}
        infs = {graph[n]: _NODES[n][0] for n in _NODES}
        for pk in expected:
            by_inf.setdefault(infs[pk], []).append(pk)
        expected = [pk for inf in sorted(by_inf) for pk in by_inf[inf]]

    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            return await _walk(
                client, graph["subject"], kind, limit, order=order, **params
            )

    pages = asyncio.run(body())
    walked = [i["pubkey"] for p in pages for i in p["items"]]
    assert walked == expected
    assert all(p["total"] == len(expected) for p in pages)
    assert all(len(p["items"]) == limit for p in pages[:-1])
    # A full final page still hands out a cursor; the next call comes back empty.
    if len(expected) % limit == 0:
        assert pages[-1]["items"] == []
    else:
        assert len(pages[-1]["items"]) == len(expected) % limit


@pytest.mark.parametrize("kind, params, with_muter", _LISTS)
def test_rows_carry_live_reporters_and_flagged_tier(graph, kind, params, with_muter):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            return await fetch_connections(
                client, graph["subject"], kind, limit=1, **params
            )

    page = asyncio.run(body())
    assert page["items"] == [
        {
            "pubkey": graph["f_015"],
            "influence": 0.015,
            "trusted_reporters": 2,
            "tier": FLAGGED,
        }
    ]
    assert page["next_cursor"]
    assert page["total"] is None


@pytest.mark.parametrize("kind, params, with_muter", _LISTS)
def test_no_flagged_connections_is_an_empty_page(graph, kind, params, with_muter):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            return await fetch_connections(
                client, graph["lonely"], kind, limit=2, with_total=True, **params
            )

    page = asyncio.run(body())
    assert page["items"] == []
    assert page["next_cursor"] is None
    assert page["total"] == 0
