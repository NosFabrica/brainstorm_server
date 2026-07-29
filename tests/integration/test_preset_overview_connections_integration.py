"""Integration tests for GET /user/{pubkey}/overview and /connections.

The companion to ``test_preset_stats_integration``: the same observer-saved
preset that drives /stats must drive these two as well, so all three read
surfaces agree and the client never sends a threshold. Requires the local stack
(Neo4j at ``settings.neo4j_db_url``; /overview also does Redis SCARDs for its
inbound counts, which the assertions here deliberately don't depend on)::

    poetry run pytest tests/integration/test_preset_overview_connections_integration.py -m integration

Observer is the platform default (anonymous request), so no auth is involved.

Issue: .scratch/preset-verified-counts/issues/03-preset-drive-overview-connections.md
"""

import asyncio

import pytest

from app.repos.user_repo import (
    get_counts_and_influence,
    get_outbound_counts_and_influence,
)
from app.services.verified_cutoffs import VerifiedCutoffs
from app.utils.observer import default_observer_pubkey
from tests.integration.preset_graph import (
    DEFAULT_CUTOFFS,
    RESTRICTIVE_CUTOFFS,
    api,
    fetch_connections,
    fetch_overview,
    fresh_driver,
    seed_graph,
    fetch_stats,
)

pytestmark = pytest.mark.integration

# node name -> (influence, trusted_reporters). influence None = property absent.
#
# Tuned so that moving DEFAULT (follower 0.02 / muter 0.01 / reporter 0.1) →
# RESTRICTIVE (all 0.5) changes every surface under test: the verified-only
# list shrinks, tier buckets fall through, `f_medium_low` crosses into flagged,
# and the subject itself becomes flagged from the observer's perspective.
_NODES: dict[str, tuple[float | None, int]] = {
    # 2 trusted reporters, so the subject is flagged once the line passes 0.4.
    "subject": (0.4, 2),
    # followed_by — one per tier band, plus the strict-`>` edge case, a node
    # that only flags under a strict preset, an always-flagged one, and one
    # with no influence property at all.
    "f_high": (0.6, 0),
    # Sits exactly ON the RESTRICTIVE line: unverified there (strict `>`), so
    # its 2 trusted reporters must put it in the flagged bucket rather than
    # leaving it in no bucket at all.
    "f_at_restrictive_line": (0.5, 2),
    "f_medium_high": (0.3, 0),
    "f_medium": (0.1, 0),
    "f_medium_low": (0.03, 2),
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
            "f_at_restrictive_line",
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

_SECTIONS = (
    "followed_by",
    "following",
    "muted_by",
    "muting",
    "reported_by",
    "reporting",
)

_TIERS = (
    "high",
    "medium_high",
    "medium",
    "medium_low",
    "low",
    "low_and_reported_by_2_or_more_trusted_pubkeys",
)


@pytest.fixture(scope="module")
def graph():
    """Seed the fixture graph; yield {name: hex_pubkey}; clean up after."""
    yield from seed_graph(_NODES, _EDGES)


async def _verified_total(client, pubkey: str, kind: str) -> int:
    page = await fetch_connections(
        client, pubkey, kind, verified_only=True, with_total=True
    )
    return page["total"]


# ---------------------------------------------------------------------------
# /overview
# ---------------------------------------------------------------------------
def test_overview_flagged_derives_from_the_saved_preset(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            default = await fetch_overview(client, graph["subject"])
        async with api(RESTRICTIVE_CUTOFFS) as client:
            restrictive = await fetch_overview(client, graph["subject"])

        # Line 0.02: only f_flagged (0.005, 3 reporters) is below it with 2+
        # trusted reporters. The subject's own 0.4 clears it.
        assert default["flagged_count"] == 1
        assert default["flagged_by_observer"] is False

        # Line 0.5: f_medium_low (0.03) and f_at_restrictive_line (0.5, which
        # the strict `>` leaves unverified) fall through too, and the subject
        # itself (0.4, 2 reporters) is now flagged.
        assert restrictive["flagged_count"] == 3
        assert restrictive["flagged_by_observer"] is True

    asyncio.run(body())


def test_overview_reports_the_subjects_own_tier(graph):
    """The subject's own bucket, so a profile page renders its tier badge from
    the observer's preset instead of re-deriving one from a threshold."""

    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            default = await fetch_overview(client, graph["subject"])
        async with api(RESTRICTIVE_CUTOFFS) as client:
            restrictive = await fetch_overview(client, graph["subject"])

        # 0.4 clears the DEFAULT line (0.02) and bands into medium_high
        # (>= 0.2, < 0.5).
        assert default["tier"] == "medium_high"

        # The RESTRICTIVE line (0.5) is above it, so it falls through the bands
        # — and its 2 trusted reporters put it in the flagged bucket.
        assert restrictive["tier"] == "low_and_reported_by_2_or_more_trusted_pubkeys"

    asyncio.run(body())


def test_lean_counts_query_agrees_with_the_full_overview_query(graph):
    """ORE-02 takes a lean query that skips the flagged scan and needs no
    verified line. The influence and outbound counts it returns must still be
    the ones `/overview` reports, or the two would quietly diverge."""

    async def body():
        observer = default_observer_pubkey()
        driver = fresh_driver()
        try:
            async with driver.session() as session:
                lean = await get_counts_and_influence(
                    session, graph["subject"], f"influence_{observer}"
                )
                full = await get_outbound_counts_and_influence(
                    session,
                    graph["subject"],
                    f"influence_{observer}",
                    f"trusted_reporters_{observer}",
                    verified_line=DEFAULT_CUTOFFS.verified_line,
                )
        finally:
            await driver.close()

        assert lean.influence == full.influence
        assert (lean.following, lean.muting, lean.reporting) == (
            full.following,
            full.muting,
            full.reporting,
        )
        # And they're real numbers, not two matching zeroes.
        assert lean.following == 2

    asyncio.run(body())


def test_overview_ignores_the_verified_threshold_query_param(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            baseline = await fetch_overview(client, graph["subject"])
            # A threshold that would flag everything if it were still honoured.
            spiked = await fetch_overview(
                client, graph["subject"], verified_threshold=0.9
            )
            assert spiked == baseline

    asyncio.run(body())


# ---------------------------------------------------------------------------
# /connections — the verified-only list filter
# ---------------------------------------------------------------------------
def test_verified_only_inbound_uses_the_sections_own_cutoff(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            subject = graph["subject"]

            # follower cutoff 0.02: 0.6, 0.5, 0.3, 0.1, 0.03 clear it; 0.02
            # (exactly at the line), 0.01, 0.005 and the null do not.
            assert await _verified_total(client, subject, "followed_by") == 5
            # muter cutoff 0.01 → 0.05 and 0.015. The follower cutoff says 1.
            assert await _verified_total(client, subject, "muted_by") == 2
            # reporter cutoff 0.1 → only 0.3. The follower cutoff says 2.
            assert await _verified_total(client, subject, "reported_by") == 1

    asyncio.run(body())


def test_verified_only_outbound_uses_the_follower_cutoff(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            # Each outbound section has one target at 0.05 and one at 0.015.
            # Follower cutoff 0.02 → exactly 1 everywhere. The muter cutoff
            # (0.01) would give 2 for `muting`; the reporter cutoff (0.1) 0.
            for kind in ("following", "muting", "reporting"):
                assert await _verified_total(client, graph["subject"], kind) == 1, kind

    asyncio.run(body())


def test_verified_only_filter_is_strict_greater_than(graph):
    async def body():
        # A cutoff placed exactly on a seeded influence must exclude it.
        cutoffs = VerifiedCutoffs(follower=0.03, muter=0.01, reporter=0.1)
        async with api(cutoffs) as client:
            page = await fetch_connections(
                client, graph["subject"], "followed_by", verified_only=True
            )
            influences = sorted(item["influence"] for item in page["items"])
            assert influences == [0.1, 0.3, 0.5, 0.6]

    asyncio.run(body())


_BANDED_TIERS = {"high", "medium_high", "medium", "medium_low"}


def test_unfiltered_list_membership_is_unaffected_by_the_preset(graph):
    """The preset decides which rows are *banded*, not which rows are in the
    section — so an unfiltered page holds the same subjects either way, and only
    the per-row tiers move."""

    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            default = await fetch_connections(
                client, graph["subject"], "followed_by", with_total=True
            )
        async with api(RESTRICTIVE_CUTOFFS) as client:
            restrictive = await fetch_connections(
                client, graph["subject"], "followed_by", with_total=True
            )

        assert default["total"] == 9
        assert restrictive["total"] == default["total"]
        assert [item["pubkey"] for item in restrictive["items"]] == [
            item["pubkey"] for item in default["items"]
        ]
        banded = lambda page: sum(  # noqa: E731
            item["tier"] in _BANDED_TIERS for item in page["items"]
        )
        assert banded(restrictive) < banded(default)

    asyncio.run(body())


# ---------------------------------------------------------------------------
# /connections — tier computation
# ---------------------------------------------------------------------------
def test_connections_tier_filter_matches_stats_tier_counts(graph):
    async def body():
        for cutoffs in (DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS):
            async with api(cutoffs) as client:
                expected = (await fetch_stats(client, graph["subject"]))["followed_by"][
                    "tier_counts"
                ]
                for tier in _TIERS:
                    page = await fetch_connections(
                        client,
                        graph["subject"],
                        "followed_by",
                        tier=tier,
                        with_total=True,
                    )
                    assert page["total"] == expected[tier], (cutoffs, tier)
                    assert len(page["items"]) == expected[tier], (cutoffs, tier)

    asyncio.run(body())


def test_raising_the_line_falls_tier_filters_through_to_unverified(graph):
    async def body():
        async with api(RESTRICTIVE_CUTOFFS) as client:
            subject = graph["subject"]

            async def total(tier: str) -> int:
                page = await fetch_connections(
                    client, subject, "followed_by", tier=tier, with_total=True
                )
                return page["total"]

            # Line 0.5, so only 0.6 stays banded — 0.5 (on the line), 0.3, 0.1
            # and 0.03 fall through to low/flagged even though a fixed band
            # would place the first three.
            assert await total("high") == 1
            assert await total("medium_high") == 0
            assert await total("medium") == 0
            assert await total("medium_low") == 0
            assert await total("low") == 5
            assert await total("low_and_reported_by_2_or_more_trusted_pubkeys") == 3

    asyncio.run(body())


def test_tier_filters_partition_the_section(graph):
    async def body():
        for cutoffs in (DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS):
            async with api(cutoffs) as client:
                unfiltered = await fetch_connections(
                    client, graph["subject"], "followed_by", with_total=True
                )
                seen: list[str] = []
                for tier in _TIERS:
                    page = await fetch_connections(
                        client, graph["subject"], "followed_by", tier=tier
                    )
                    seen.extend(item["pubkey"] for item in page["items"])

                # Every subject lands in exactly one bucket, including the one
                # sitting exactly on the RESTRICTIVE line.
                assert sorted(seen) == sorted(
                    item["pubkey"] for item in unfiltered["items"]
                ), cutoffs
                assert len(seen) == unfiltered["total"], cutoffs

    asyncio.run(body())


# ---------------------------------------------------------------------------
# /connections — the per-row tier
# ---------------------------------------------------------------------------
def test_row_tier_matches_the_tier_filter_buckets(graph):
    async def body():
        for cutoffs in (DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS):
            async with api(cutoffs) as client:
                rows = await fetch_connections(client, graph["subject"], "followed_by")
                by_row_tier: dict[str, set[str]] = {}
                for item in rows["items"]:
                    by_row_tier.setdefault(item["tier"], set()).add(item["pubkey"])

                for tier in _TIERS:
                    page = await fetch_connections(
                        client, graph["subject"], "followed_by", tier=tier
                    )
                    assert by_row_tier.get(tier, set()) == {
                        item["pubkey"] for item in page["items"]
                    }, (cutoffs, tier)

    asyncio.run(body())


def test_flagged_rows_carry_the_flagged_tier(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            page = await fetch_connections(client, graph["subject"], "flagged")
            assert page["items"]
            for item in page["items"]:
                assert item["tier"] == "low_and_reported_by_2_or_more_trusted_pubkeys"

    asyncio.run(body())


def test_connections_ignores_the_verified_threshold_query_param(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            subject = graph["subject"]
            for kind, extra in (
                ("followed_by", {"verified_only": True}),
                ("followed_by", {"tier": "low"}),
                ("flagged", {}),
            ):
                baseline = await fetch_connections(
                    client, subject, kind, with_total=True, **extra
                )
                spiked = await fetch_connections(
                    client,
                    subject,
                    kind,
                    with_total=True,
                    verified_threshold=0.9,
                    **extra,
                )
                assert spiked == baseline, (kind, extra)

    asyncio.run(body())


# ---------------------------------------------------------------------------
# The three read surfaces agree, and move together
# ---------------------------------------------------------------------------
def test_verified_counts_agree_between_stats_and_connections(graph):
    async def body():
        for cutoffs in (DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS):
            async with api(cutoffs) as client:
                stats = await fetch_stats(client, graph["subject"])
                for kind in _SECTIONS:
                    assert await _verified_total(client, graph["subject"], kind) == (
                        stats[kind]["verified"]
                    ), (cutoffs, kind)

    asyncio.run(body())


def test_flagged_agrees_between_overview_and_connections(graph):
    async def body():
        for cutoffs in (DEFAULT_CUTOFFS, RESTRICTIVE_CUTOFFS):
            async with api(cutoffs) as client:
                overview = await fetch_overview(client, graph["subject"])
                page = await fetch_connections(
                    client, graph["subject"], "flagged", with_total=True
                )
                assert overview["flagged_count"] == page["total"], cutoffs
                assert len(page["items"]) == overview["flagged_count"], cutoffs

    asyncio.run(body())


def test_switching_preset_moves_all_three_surfaces_together(graph):
    async def body():
        async with api(DEFAULT_CUTOFFS) as client:
            subject = graph["subject"]
            default_stats = await fetch_stats(client, subject)
            default_verified = {
                kind: await _verified_total(client, subject, kind) for kind in _SECTIONS
            }
            default_overview = await fetch_overview(client, subject)
        async with api(RESTRICTIVE_CUTOFFS) as client:
            restrictive_stats = await fetch_stats(client, subject)
            restrictive_verified = {
                kind: await _verified_total(client, subject, kind) for kind in _SECTIONS
            }
            restrictive_overview = await fetch_overview(client, subject)

        for kind in _SECTIONS:
            assert restrictive_stats[kind]["verified"] < default_stats[kind]["verified"]
            assert restrictive_verified[kind] < default_verified[kind], kind

        assert restrictive_overview["flagged_count"] > default_overview["flagged_count"]

    asyncio.run(body())
