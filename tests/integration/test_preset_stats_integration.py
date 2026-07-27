"""Integration tests for GET /user/{pubkey}/stats against the real local Neo4j.

Requires the local stack (Neo4j reachable at ``settings.neo4j_db_url`` — on this
machine ``bolt://localhost:7688`` via ``.env``). Run explicitly with::

    poetry run pytest tests/integration/test_preset_stats_integration.py -m integration

Seeds a synthetic graph around one `subject`, with `influence_<observer>` and
`trusted_reporters_<observer>` set per node, and asserts the per-section
verified counts and tier buckets the endpoint derives from the *observer's saved
preset* (the `get_verified_cutoffs` dependency is overridden here so the test
needs Neo4j only, not Postgres — the preset→cutoff resolution itself is covered
by ``tests/test_verified_cutoffs.py``).

Observer is the platform default (anonymous request), so no auth is involved.

Issue: .scratch/preset-verified-counts/issues/02-preset-drive-stats.md
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

import app.services.user_service as user_service_module
from app.api import app
from app.core.config import settings
from app.routers.user.dependencies import get_verified_cutoffs
from app.services.verified_cutoffs import VerifiedCutoffs
from app.utils.observer import default_observer_pubkey
from tests.test_verified_cutoffs import SEED

pytestmark = pytest.mark.integration


def _seeded(preset: str) -> VerifiedCutoffs:
    row = SEED[preset]
    return VerifiedCutoffs(
        follower=row["verified_followers_influence_cutoff"],
        muter=row["verified_muters_influence_cutoff"],
        reporter=row["verified_reporters_influence_cutoff"],
    )


# The real factory presets, so the fixture influences below stay meaningful.
DEFAULT_CUTOFFS = _seeded("DEFAULT")  # follower 0.02, muter 0.01, reporter 0.1
RESTRICTIVE_CUTOFFS = _seeded("RESTRICTIVE")  # all 0.5

# node name -> (influence, trusted_reporters). influence None = property absent.
_NODES: dict[str, tuple[float | None, int]] = {
    "subject": (0.4, 0),
    # followed_by — one per tier band, plus the strict-`>` edge case, a flagged
    # node and one with no influence property at all.
    "f_high": (0.6, 0),
    "f_medium_high": (0.3, 0),
    "f_medium": (0.1, 0),
    "f_medium_low": (0.03, 0),
    "f_at_cutoff": (0.02, 0),  # exactly at DEFAULT's line — strict `>` excludes
    "f_low": (0.01, 0),
    "f_flagged": (0.005, 3),
    "f_no_influence": (None, 0),
    # muted_by — 0.015 sits between the muter cutoff (0.01) and the follower
    # cutoff (0.02), so it discriminates which cutoff the section used.
    "m_in_hi": (0.05, 0),
    "m_in_mid": (0.015, 0),
    "m_in_lo": (0.005, 0),
    # reported_by — 0.05 sits between the follower cutoff and the reporter
    # cutoff (0.1), same trick in the other direction.
    "r_in_hi": (0.3, 0),
    "r_in_mid": (0.05, 0),
    "r_in_lo": (0.005, 0),
    # outbound targets — all three sections must use the FOLLOWER cutoff.
    "out_follow_hi": (0.05, 0),
    "out_follow_lo": (0.015, 0),
    "out_mute_hi": (0.05, 0),
    "out_mute_lo": (0.015, 0),
    "out_report_hi": (0.05, 0),
    "out_report_lo": (0.015, 0),
}

_EDGES: list[tuple[str, str, str]] = [
    *[
        (n, "FOLLOWS", "subject")
        for n in (
            "f_high",
            "f_medium_high",
            "f_medium",
            "f_medium_low",
            "f_at_cutoff",
            "f_low",
            "f_flagged",
            "f_no_influence",
        )
    ],
    *[(n, "MUTES", "subject") for n in ("m_in_hi", "m_in_mid", "m_in_lo")],
    *[(n, "REPORTS", "subject") for n in ("r_in_hi", "r_in_mid", "r_in_lo")],
    *[("subject", "FOLLOWS", n) for n in ("out_follow_hi", "out_follow_lo")],
    *[("subject", "MUTES", n) for n in ("out_mute_hi", "out_mute_lo")],
    *[("subject", "REPORTS", n) for n in ("out_report_hi", "out_report_lo")],
]


def _fresh_driver():
    return AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )


@pytest.fixture(scope="module")
def graph():
    """Seed the fixture graph; yield {name: hex_pubkey}; clean up after."""
    observer = default_observer_pubkey()
    influence_key = f"influence_{observer}"
    trusted_reporters_key = f"trusted_reporters_{observer}"
    pks = {name: Keys.generate().public_key().to_hex() for name in _NODES}

    async def _seed() -> None:
        driver = _fresh_driver()
        try:
            async with driver.session() as session:
                for name, (influence, trusted_reporters) in _NODES.items():
                    await session.run(
                        f"MERGE (u:NostrUser {{pubkey: $pk}}) "
                        f"SET u.`{trusted_reporters_key}` = $tr "
                        + (
                            f"SET u.`{influence_key}` = $inf"
                            if influence is not None
                            else ""
                        ),
                        pk=pks[name],
                        inf=influence,
                        tr=trusted_reporters,
                    )
                for src, rel, dst in _EDGES:
                    await session.run(
                        f"MATCH (a:NostrUser {{pubkey: $src}}), "
                        f"(b:NostrUser {{pubkey: $dst}}) MERGE (a)-[:{rel}]->(b)",
                        src=pks[src],
                        dst=pks[dst],
                    )
        finally:
            await driver.close()

    async def _teardown() -> None:
        driver = _fresh_driver()
        try:
            async with driver.session() as session:
                await session.run(
                    "MATCH (u:NostrUser) WHERE u.pubkey IN $pubkeys DETACH DELETE u",
                    pubkeys=list(pks.values()),
                )
        finally:
            await driver.close()

    asyncio.run(_seed())
    try:
        yield pks
    finally:
        asyncio.run(_teardown())


@asynccontextmanager
async def _api(cutoffs: VerifiedCutoffs):
    """HTTP client over the app with a loop-local Neo4j driver.

    Same cross-loop caveat as ``test_shortest_path_integration`` — each test
    body runs in ONE ``asyncio.run`` loop with a fresh driver injected for its
    duration. `get_verified_cutoffs` is overridden so the observer's "saved
    preset" is whatever the test says it is, with no Postgres round-trip.
    """
    driver = _fresh_driver()
    original = user_service_module.neo4j_driver
    user_service_module.neo4j_driver = driver
    app.dependency_overrides[get_verified_cutoffs] = lambda: cutoffs
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_verified_cutoffs, None)
        user_service_module.neo4j_driver = original
        await driver.close()


async def _stats(client, pubkey: str, **params) -> dict:
    resp = await client.get(f"/user/{pubkey}/stats", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# Inbound sections use their own per-relationship cutoff
# ---------------------------------------------------------------------------
def test_inbound_sections_use_their_own_cutoff(graph):
    async def body():
        async with _api(DEFAULT_CUTOFFS) as client:
            data = await _stats(client, graph["subject"])

            # follower cutoff 0.02, strict `>`: 0.6, 0.3, 0.1, 0.03 clear it;
            # 0.02 (exactly at the line), 0.01, 0.005 and the null do not.
            assert data["followed_by"]["total"] == 8
            assert data["followed_by"]["verified"] == 4

            # muter cutoff 0.01: 0.05 and 0.015 clear it. The follower cutoff
            # (0.02) would have said 1.
            assert data["muted_by"]["total"] == 3
            assert data["muted_by"]["verified"] == 2

            # reporter cutoff 0.1: only 0.3 clears it. The follower cutoff
            # (0.02) would have said 2.
            assert data["reported_by"]["total"] == 3
            assert data["reported_by"]["verified"] == 1

    asyncio.run(body())


def test_verified_comparison_is_strict_greater_than(graph):
    async def body():
        # A cutoff placed exactly on a seeded influence must exclude it.
        cutoffs = VerifiedCutoffs(follower=0.6, muter=0.01, reporter=0.1)
        async with _api(cutoffs) as client:
            data = await _stats(client, graph["subject"])
            assert data["followed_by"]["verified"] == 0

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Outbound sections use the general trusted-account bar (the follower cutoff)
# ---------------------------------------------------------------------------
def test_outbound_sections_use_the_follower_cutoff(graph):
    async def body():
        async with _api(DEFAULT_CUTOFFS) as client:
            data = await _stats(client, graph["subject"])

            # Each outbound section has one target at 0.05 and one at 0.015.
            # Follower cutoff 0.02 → exactly 1 verified everywhere. The muter
            # cutoff (0.01) would give 2 for `muting`; the reporter cutoff
            # (0.1) would give 0 for `reporting`.
            for kind in ("following", "muting", "reporting"):
                assert data[kind]["total"] == 2, kind
                assert data[kind]["verified"] == 1, kind

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Tier buckets: fixed bands above the line, fallthrough below it
# ---------------------------------------------------------------------------
def test_tier_buckets_under_default_preset(graph):
    async def body():
        async with _api(DEFAULT_CUTOFFS) as client:
            tiers = (await _stats(client, graph["subject"]))["followed_by"][
                "tier_counts"
            ]

            assert tiers["high"] == 1  # 0.6
            assert tiers["medium_high"] == 1  # 0.3
            assert tiers["medium"] == 1  # 0.1
            assert tiers["medium_low"] == 1  # 0.03
            # 0.005 with 3 trusted reporters.
            assert tiers["low_and_reported_by_2_or_more_trusted_pubkeys"] == 1
            assert tiers["low"] == 3  # 0.02 (at the line), 0.01, no-influence
            assert sum(tiers.values()) == 8

    asyncio.run(body())


def test_raising_the_line_falls_subjects_through_to_unverified(graph):
    async def body():
        async with _api(RESTRICTIVE_CUTOFFS) as client:
            tiers = (await _stats(client, graph["subject"]))["followed_by"][
                "tier_counts"
            ]

            # Line is 0.5, so only 0.6 stays banded — everything a fixed band
            # would otherwise have placed (0.3 → medium_high, 0.1 → medium,
            # 0.03 → medium_low) falls through.
            assert tiers["high"] == 1
            assert tiers["medium_high"] == 0
            assert tiers["medium"] == 0
            assert tiers["medium_low"] == 0
            assert tiers["low_and_reported_by_2_or_more_trusted_pubkeys"] == 1
            assert tiers["low"] == 6
            assert sum(tiers.values()) == 8

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Switching the observer's preset moves the counts
# ---------------------------------------------------------------------------
def test_switching_default_to_restrictive_reduces_verified_counts(graph):
    async def body():
        async with _api(DEFAULT_CUTOFFS) as client:
            default = await _stats(client, graph["subject"])
        async with _api(RESTRICTIVE_CUTOFFS) as client:
            restrictive = await _stats(client, graph["subject"])

        for kind in (
            "followed_by",
            "following",
            "muted_by",
            "muting",
            "reported_by",
            "reporting",
        ):
            assert restrictive[kind]["total"] == default[kind]["total"], kind
            assert restrictive[kind]["verified"] < default[kind]["verified"], kind

        # The unverified bucket grows by exactly what the bands gave up.
        assert (
            restrictive["followed_by"]["tier_counts"]["low"]
            > default["followed_by"]["tier_counts"]["low"]
        )

    asyncio.run(body())


# ---------------------------------------------------------------------------
# The client-supplied threshold is gone
# ---------------------------------------------------------------------------
def test_verified_threshold_query_param_is_ignored(graph):
    async def body():
        async with _api(DEFAULT_CUTOFFS) as client:
            baseline = await _stats(client, graph["subject"])
            # A threshold that would zero every count if it were still honoured.
            spiked = await _stats(client, graph["subject"], verified_threshold=0.9)
            assert spiked == baseline

    asyncio.run(body())
