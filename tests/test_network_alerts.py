"""Fast-suite tests for GET /networkAlerts.

Covers everything that needs no graph backend: input validation, npub
resolution, the stored-vs-counted follower-count hybrid, threshold arithmetic,
section split, ordering, limits, and inf/nan coercion.

The Cypher itself — REPORTS anchoring, the capped LIMIT, and live-edge section
membership — is exercised against a real Neo4j in
tests/integration/test_network_alerts_integration.py.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from nostr_sdk import Keys

from app.api import app
from app.routers.network_alerts.dependencies import get_alert_cutoffs
from app.services.verified_cutoffs import VerifiedCutoffs
from app.services.network_alerts_service import (
    follower_count_cap_for,
    reporter_threshold_for,
)


def _get(client, params: dict):
    return client.get("/networkAlerts", params=params)


def _pk() -> str:
    return Keys.generate().public_key().to_hex()


def _candidate(pubkey: str, **overrides) -> dict:
    """A row as get_network_alert_candidates returns it."""
    row = {
        "pubkey": pubkey,
        "verified_reporters": 5,
        "stored_followers": 0,
        "influence": 0.5,
        "hops": 1,
        "is_direct": True,
    }
    row.update(overrides)
    return row


@pytest.fixture
def mock_graph(monkeypatch):
    """Patch the three repo calls, the Neo4j session opener, and the preset read.

    Returns a dict of the AsyncMocks so a test can set candidates and, where it
    matters, the capped-count and muter-count responses. `cutoffs` is the
    observer's resolved preset — set `.return_value` to a different
    `VerifiedCutoffs` to exercise a non-DEFAULT observer without Postgres.

    Only the cutoffs dependency is overridden, not observer resolution, so the
    malformed-pubkey and npub cases still run the real validator.
    """
    mocks = {
        "candidates": AsyncMock(return_value=[]),
        "capped": AsyncMock(return_value={}),
        "muters": AsyncMock(return_value={}),
        # DEFAULT's seeded values.
        "cutoffs": AsyncMock(
            return_value=VerifiedCutoffs(follower=0.02, muter=0.01, reporter=0.1)
        ),
    }
    # A plain function, not the AsyncMock: FastAPI reads the override's
    # signature, and AsyncMock's (*args, **kwargs) becomes required query params.
    async def _cutoffs_override():
        return await mocks["cutoffs"]()

    app.dependency_overrides[get_alert_cutoffs] = _cutoffs_override
    monkeypatch.setattr(
        "app.services.network_alerts_service.get_network_alert_candidates",
        mocks["candidates"],
    )
    monkeypatch.setattr(
        "app.services.network_alerts_service.count_above_cutoff_followers_capped",
        mocks["capped"],
    )
    monkeypatch.setattr(
        "app.services.network_alerts_service.count_verified_muters", mocks["muters"]
    )

    @asynccontextmanager
    async def _fake_session():
        yield AsyncMock()

    fake_driver = MagicMock()
    fake_driver.session = lambda: _fake_session()
    monkeypatch.setattr("app.services.network_alerts_service.neo4j_driver", fake_driver)
    yield mocks
    app.dependency_overrides.pop(get_alert_cutoffs, None)


# ---------------------------------------------------------------------------
# Threshold arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "followers,expected",
    [(0, 2), (499, 2), (500, 3), (999, 3), (1000, 4), (2500, 7), (8600, 19)],
)
def test_reporter_threshold_scales_per_500_followers(followers, expected):
    assert reporter_threshold_for(followers) == expected


@pytest.mark.parametrize("verified_reporters", range(3, 40))
def test_cap_is_exactly_the_point_where_a_row_can_no_longer_alert(
    verified_reporters,
):
    """The cap's correctness invariant, stated directly.

    A row alerts iff its verified follower count is strictly below the cap. So
    a count that reaches the cap can be abandoned — it can only belong to a row
    that fails — and a count below the cap is both exact and passing.
    """
    cap = follower_count_cap_for(verified_reporters)

    assert verified_reporters > reporter_threshold_for(cap - 1)
    assert not verified_reporters > reporter_threshold_for(cap)


def test_cap_is_never_below_one_bucket():
    """Candidates are prefiltered to >= 3 reporters, so the cap can't be 0."""
    assert follower_count_cap_for(3) == 500


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "not-a-pubkey",
        "nprofile1qqstest",  # other NIP-19 forms are rejected
        "8f3fbb5129dcb9194c02b67c1b41a3f60dd369663f53499b2b3c72a73c6fa9",  # 62 chars
        "npub1invalidinvalidinvalid",
    ],
)
def test_invalid_observer_is_400(client, mock_graph, bad):
    resp = _get(client, {"observer": bad})

    assert resp.status_code == 400
    assert "observer" in resp.json()["detail"]


def test_observer_is_required(client, mock_graph):
    assert _get(client, {}).status_code == 422


def test_accepts_npub_and_echoes_canonical_hex(client, mock_graph):
    keys = Keys.generate()

    resp = _get(client, {"observer": keys.public_key().to_bech32()})

    assert resp.status_code == 200
    assert resp.json()["data"]["observerPubkey"] == keys.public_key().to_hex()


def test_observer_drives_the_property_keys(client, mock_graph):
    observer = _pk()

    _get(client, {"observer": observer})

    kwargs = mock_graph["candidates"].await_args.kwargs
    assert kwargs["observer_pubkey"] == observer
    assert kwargs["influence_key"] == f"influence_{observer}"
    assert kwargs["hops_key"] == f"hops_{observer}"
    assert kwargs["trusted_followers_key"] == f"trusted_followers_{observer}"
    assert kwargs["trusted_reporters_key"] == f"trusted_reporters_{observer}"


@pytest.mark.parametrize("bad_limit", [0, -1, 501])
def test_limit_out_of_range_is_422(client, mock_graph, bad_limit):
    assert _get(client, {"observer": _pk(), "limit": bad_limit}).status_code == 422


# ---------------------------------------------------------------------------
# Stored count vs counted fallback
# ---------------------------------------------------------------------------
def test_stored_follower_count_skips_the_graph_scan(client, mock_graph):
    bob = _pk()
    mock_graph["candidates"].return_value = [
        _candidate(bob, stored_followers=1200, verified_reporters=5)
    ]

    data = _get(client, {"observer": _pk()}).json()["data"]

    mock_graph["capped"].assert_not_awaited()
    item = data["directFollows"][0]
    assert item["verifiedFollowerCount"] == 1200
    assert item["reporterThreshold"] == 4


def test_missing_stored_count_falls_back_to_a_capped_scan(client, mock_graph):
    bob = _pk()
    mock_graph["candidates"].return_value = [
        _candidate(bob, stored_followers=None, verified_reporters=5)
    ]
    # Cap for 5 reporters is 500 * 3 = 1500; came back under it, so it's exact.
    mock_graph["capped"].return_value = {bob: 900}

    data = _get(client, {"observer": _pk()}).json()["data"]

    assert mock_graph["capped"].await_args.kwargs["cap"] == 1500
    assert mock_graph["capped"].await_args.kwargs["pubkeys"] == [bob]
    item = data["directFollows"][0]
    assert item["verifiedFollowerCount"] == 900
    assert item["reporterThreshold"] == 3


def test_count_that_reaches_the_cap_is_dropped_not_reported(client, mock_graph):
    """Hitting the cap means the true count is >= cap, so the row can't alert.

    Reporting it would mean publishing a truncated follower count as if it were
    real; dropping it is both correct and keeps the payload honest.
    """
    bob = _pk()
    mock_graph["candidates"].return_value = [
        _candidate(bob, stored_followers=None, verified_reporters=5)
    ]
    mock_graph["capped"].return_value = {bob: 1500}  # == cap

    data = _get(client, {"observer": _pk()}).json()["data"]

    assert data["directFollows"] == []
    assert data["extendedNetwork"] == []


def test_fallback_buckets_candidates_by_cap(client, mock_graph):
    """One query per distinct cap — Cypher can't vary a LIMIT per row."""
    low, high = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(low, stored_followers=None, verified_reporters=3),
        _candidate(high, stored_followers=None, verified_reporters=10),
    ]
    mock_graph["capped"].return_value = {}

    _get(client, {"observer": _pk()})

    caps = {c.kwargs["cap"] for c in mock_graph["capped"].await_args_list}
    assert caps == {500, 4000}


def test_stored_and_missing_counts_mix_in_one_response(client, mock_graph):
    stored, counted = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(stored, stored_followers=600, verified_reporters=9),
        _candidate(counted, stored_followers=None, verified_reporters=9),
    ]
    mock_graph["capped"].return_value = {counted: 100}

    data = _get(client, {"observer": _pk()}).json()["data"]

    # Only the one lacking a stored value was scanned.
    assert mock_graph["capped"].await_args.kwargs["pubkeys"] == [counted]
    by_pubkey = {i["pubkey"]: i for i in data["directFollows"]}
    assert by_pubkey[stored]["verifiedFollowerCount"] == 600
    assert by_pubkey[counted]["verifiedFollowerCount"] == 100


# ---------------------------------------------------------------------------
# Threshold application, sections, ordering
# ---------------------------------------------------------------------------
def test_report_count_must_strictly_exceed_threshold(client, mock_graph):
    at, over = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(at, stored_followers=0, verified_reporters=2),
        _candidate(over, stored_followers=0, verified_reporters=3),
    ]

    data = _get(client, {"observer": _pk()}).json()["data"]

    assert [i["pubkey"] for i in data["directFollows"]] == [over]


def test_sections_split_on_is_direct(client, mock_graph):
    direct, extended = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(direct, stored_followers=0, is_direct=True),
        _candidate(extended, stored_followers=0, is_direct=False, hops=3),
    ]

    data = _get(client, {"observer": _pk()}).json()["data"]

    assert [i["pubkey"] for i in data["directFollows"]] == [direct]
    assert [i["pubkey"] for i in data["extendedNetwork"]] == [extended]


def test_direct_section_orders_by_report_count_desc(client, mock_graph):
    few, many = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(few, stored_followers=0, verified_reporters=4),
        _candidate(many, stored_followers=0, verified_reporters=12),
    ]

    data = _get(client, {"observer": _pk()}).json()["data"]

    assert [i["pubkey"] for i in data["directFollows"]] == [many, few]


def test_extended_section_orders_by_influence_desc(client, mock_graph):
    low, high = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(low, stored_followers=0, is_direct=False, influence=0.05),
        _candidate(high, stored_followers=0, is_direct=False, influence=0.60),
    ]

    data = _get(client, {"observer": _pk()}).json()["data"]

    assert [i["pubkey"] for i in data["extendedNetwork"]] == [high, low]


def test_item_carries_every_score_the_panel_needs(client, mock_graph):
    bob = _pk()
    mock_graph["candidates"].return_value = [
        _candidate(
            bob, stored_followers=1200, verified_reporters=9, influence=0.31, hops=1
        )
    ]
    mock_graph["muters"].return_value = {bob: 7}

    item = _get(client, {"observer": _pk()}).json()["data"]["directFollows"][0]

    assert item == {
        "pubkey": bob,
        "influence": 0.31,
        "hops": 1,
        "verifiedFollowerCount": 1200,
        "verifiedMuterCount": 7,
        "verifiedReporterCount": 9,
        # Returned alongside the count so the UI can say *why* it's flagged.
        "reporterThreshold": 4,
    }


def test_muters_are_counted_only_for_returned_rows(client, mock_graph):
    kept, dropped = _pk(), _pk()
    mock_graph["candidates"].return_value = [
        _candidate(kept, stored_followers=0, verified_reporters=5),
        # Below threshold — never reaches the payload, so never counted.
        _candidate(dropped, stored_followers=0, verified_reporters=2),
    ]

    _get(client, {"observer": _pk()})

    assert mock_graph["muters"].await_args.kwargs["pubkeys"] == [kept]


# ---------------------------------------------------------------------------
# The observer's saved preset drives every cutoff
# ---------------------------------------------------------------------------
def test_resolved_cutoffs_drive_both_graph_queries(client, mock_graph):
    """Whatever the preset resolves to reaches the candidate and count queries."""
    observer = _pk()
    mock_graph["cutoffs"].return_value = VerifiedCutoffs(
        follower=0.5, muter=0.5, reporter=0.5
    )
    mock_graph["candidates"].return_value = [
        _candidate(_pk(), stored_followers=None, verified_reporters=5)
    ]

    _get(client, {"observer": observer})

    assert mock_graph["candidates"].await_args.kwargs["cutoff"] == 0.5
    assert mock_graph["capped"].await_args.kwargs["cutoff"] == 0.5


def test_the_cutoffs_dependency_resolves_the_named_observers_preset(monkeypatch):
    """The half `mock_graph` overrides, tested directly: whose preset gets read.

    Asserted here rather than through the app because the fixture replaces this
    dependency wholesale.
    """
    keys = Keys.generate()
    resolved = AsyncMock(
        return_value=VerifiedCutoffs(follower=0.5, muter=0.5, reporter=0.5)
    )
    monkeypatch.setattr(
        "app.routers.network_alerts.dependencies.resolve_verified_cutoffs", resolved
    )
    db = object()

    cutoffs = asyncio.run(
        get_alert_cutoffs(observer=keys.public_key().to_hex(), db=db)
    )

    assert cutoffs.follower == 0.5
    assert resolved.await_args.args == (db, keys.public_key().to_hex())


def test_muter_count_uses_the_muter_cutoff_not_the_follower_line(
    client, mock_graph
):
    """Muters ride their own preset cutoff (0.01 on DEFAULT vs the follower
    line's 0.02), matching VerifiedCutoffs.for_kind("muted_by"). Sharing one
    value here would disagree with /stats."""
    mock_graph["candidates"].return_value = [
        _candidate(_pk(), stored_followers=0, verified_reporters=5)
    ]

    _get(client, {"observer": _pk()})

    assert mock_graph["muters"].await_args.kwargs["verified_threshold"] == 0.01
    assert mock_graph["candidates"].await_args.kwargs["cutoff"] == 0.02


# ---------------------------------------------------------------------------
# Limits and edge cases
# ---------------------------------------------------------------------------
def test_limit_caps_each_section_independently(client, mock_graph):
    mock_graph["candidates"].return_value = [
        _candidate(_pk(), stored_followers=0) for _ in range(3)
    ] + [_candidate(_pk(), stored_followers=0, is_direct=False)]

    data = _get(client, {"observer": _pk(), "limit": 2}).json()["data"]

    assert len(data["directFollows"]) == 2
    assert data["directFollowsTruncated"] is True
    assert len(data["extendedNetwork"]) == 1
    assert data["extendedNetworkTruncated"] is False


def test_section_filled_exactly_to_limit_is_not_truncated(client, mock_graph):
    """Truncation is 'more exist behind this', not 'the page is full'."""
    mock_graph["candidates"].return_value = [
        _candidate(_pk(), stored_followers=0) for _ in range(2)
    ]

    data = _get(client, {"observer": _pk(), "limit": 2}).json()["data"]

    assert len(data["directFollows"]) == 2
    assert data["directFollowsTruncated"] is False


def test_no_candidates_returns_empty_sections(client, mock_graph):
    data = _get(client, {"observer": _pk()}).json()["data"]

    assert data["directFollows"] == []
    assert data["extendedNetwork"] == []
    assert data["directFollowsTruncated"] is False
    assert data["extendedNetworkTruncated"] is False


@pytest.mark.parametrize("bad_influence", [float("inf"), float("-inf"), float("nan")])
def test_unserializable_influence_becomes_null(client, mock_graph, bad_influence):
    """Neo4j hands back inf/nan for stale properties; json.dumps rejects both."""
    mock_graph["candidates"].return_value = [
        _candidate(_pk(), stored_followers=0, influence=bad_influence)
    ]

    resp = _get(client, {"observer": _pk()})

    assert resp.status_code == 200
    assert resp.json()["data"]["directFollows"][0]["influence"] is None


def test_missing_hops_is_tolerated(client, mock_graph):
    """hops_<observer> is absent until the observer's first GrapeRank run."""
    mock_graph["candidates"].return_value = [
        _candidate(_pk(), stored_followers=0, hops=None)
    ]

    item = _get(client, {"observer": _pk()}).json()["data"]["directFollows"][0]

    assert item["hops"] is None
    assert item["verifiedReporterCount"] == 5
