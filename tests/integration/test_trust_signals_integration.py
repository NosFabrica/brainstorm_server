"""Integration tests for POST /user/trustSignals.

A list surface draws a ring and a Flagged chip per author. The batch must say
exactly what /overview says for each pubkey, for the same Observer and line::

    poetry run pytest tests/integration/test_trust_signals_integration.py -m integration
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from nostr_sdk import Keys

from app.api import app
from app.utils.api_validators import verify_token_optional
from app.utils.auth.auth_models import JWTData
from tests.integration.preset_graph import (
    DEFAULT_CUTOFFS,
    RESTRICTIVE_CUTOFFS,
    api,
    fetch_overview,
    fresh_driver,
    seed_graph,
)

pytestmark = pytest.mark.integration

# node name -> (influence, trusted_reporters) under the default observer.
_NODES: dict[str, tuple[float | None, int]] = {
    "verified": (0.4, 2),  # flagged only once the line passes 0.4
    "at_default_line": (0.02, 0),  # strict `>`: not verified at 0.02
    "flagged": (0.005, 3),
    "reported_once": (0.005, 1),
    "no_influence": (None, 0),
}

# The same people as seen by a signed-in viewer's own web of trust.
_VIEWER = Keys.generate().public_key().to_hex()
_VIEWER_PROPS: dict[str, tuple[float, int]] = {
    "verified": (0.001, 4),
    "flagged": (0.9, 0),
}


@pytest.fixture(scope="module")
def graph():
    for pks in seed_graph(_NODES, []):

        async def _seed_viewer() -> None:
            driver = fresh_driver()
            try:
                async with driver.session() as session:
                    for name, (influence, reporters) in _VIEWER_PROPS.items():
                        await session.run(
                            f"MATCH (u:NostrUser {{pubkey: $pk}}) "
                            f"SET u.`influence_{_VIEWER}` = $inf, "
                            f"u.`trusted_reporters_{_VIEWER}` = $tr",
                            pk=pks[name],
                            inf=influence,
                            tr=reporters,
                        )
            finally:
                await driver.close()

        asyncio.run(_seed_viewer())
        yield pks


@pytest.fixture
def signed_in_viewer():
    app.dependency_overrides[verify_token_optional] = lambda: JWTData(
        nostr_pubkey=_VIEWER,
        expires_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    yield
    app.dependency_overrides.pop(verify_token_optional, None)


async def _signals(client, pubkeys: list[str]) -> list[dict]:
    resp = await client.post("/user/trustSignals", json={"pubkeys": pubkeys})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["results"]


async def _assert_matches_overview(client, pubkeys: list[str]) -> None:
    results = await _signals(client, pubkeys)
    assert [r["pubkey"] for r in results] == pubkeys
    for r in results:
        overview = await fetch_overview(client, r["pubkey"])
        assert r["influence"] == overview["influence"]
        assert r["flagged"] is overview["flagged_by_observer"]


@pytest.mark.parametrize("cutoffs", [DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS])
def test_anonymous_batch_matches_overview_for_every_pubkey(graph, cutoffs):
    async def body():
        async with api(cutoffs) as client:
            await _assert_matches_overview(client, list(graph.values()))

    asyncio.run(body())


def test_signed_in_batch_matches_overview_from_the_viewers_trust(
    graph, signed_in_viewer
):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            pubkeys = list(graph.values())
            await _assert_matches_overview(client, pubkeys)
            results = {r["pubkey"]: r for r in await _signals(client, pubkeys)}
        # The viewer's own props, not the house's: flipped on purpose.
        assert results[graph["verified"]]["flagged"] is True
        assert results[graph["flagged"]]["flagged"] is False
        assert results[graph["at_default_line"]]["influence"] is None

    asyncio.run(body())


def test_verified_is_strictly_above_the_line(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            results = {
                r["pubkey"]: r for r in await _signals(client, list(graph.values()))
            }
        assert results[graph["verified"]]["verified"] is True
        assert results[graph["at_default_line"]]["verified"] is False
        assert results[graph["flagged"]]["verified"] is False
        assert results[graph["reported_once"]]["flagged"] is False
        assert results[graph["no_influence"]]["verified"] is False

    asyncio.run(body())


def test_unknown_pubkey_is_unrated(graph):
    unknown = Keys.generate().public_key().to_hex()

    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            [result] = await _signals(client, [unknown])
        assert result == {
            "pubkey": unknown,
            "influence": None,
            "verified": False,
            "flagged": False,
        }

    asyncio.run(body())


def test_duplicates_collapse_and_input_order_is_kept(graph):
    a, b = graph["flagged"], graph["verified"]

    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            results = await _signals(client, [a, b, a])
        assert [r["pubkey"] for r in results] == [a, b]

    asyncio.run(body())
