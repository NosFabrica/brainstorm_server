"""End-to-end tests for the Open Ranking endpoints.

These boot a FastAPI app, route a real HTTP request through ASGI, and verify
the response per the ORE specs. The Neo4j / Redis / Vespa boundaries are
patched at import sites in the open_ranking router modules so the suite is
deterministic and infra-free.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from nostr_sdk import Keys

from tests.conftest import make_nwt


VALID_PUBKEY_1 = "a" * 64
VALID_PUBKEY_2 = "b" * 64
VALID_PUBKEY_3 = "c" * 64
POV_PUBKEY = "d" * 64
INVALID_PUBKEY = "not-a-valid-pubkey"


@contextmanager
def pov_availability(
    exists: bool = True,
    graperank: bool = True,
    search: bool = True,
    in_pipeline: bool = False,
):
    """Stub the availability gate's repo reads (patched at their import site
    in the availability module). Defaults describe a fully provisioned,
    fully computed pov."""
    with patch(
        "app.routers.open_ranking.availability.get_pov_availability_fields_on_db",
        new=AsyncMock(return_value=(exists, graperank, search)),
    ), patch(
        "app.routers.open_ranking.availability.any_request_in_pipeline_for_pubkey_on_db",
        new=AsyncMock(return_value=in_pipeline),
    ):
        yield


# ===========================================================================
# ORE-01: Capability document
# ===========================================================================
class TestCapabilityDocument:
    def test_well_known_returns_capability_doc(self, client):
        r = client.get("/.well-known/open-ranking.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        # All implemented endpoints MUST be advertised.
        for path in (
            "/stats/pubkey",
            "/rank/pubkeys",
            "/search/pubkeys",
            "/followers",
            "/muters",
        ):
            assert path in body, f"capability doc missing {path}"
            assert isinstance(body[path], list) and body[path]
            for algo in body[path]:
                assert "id" in algo

    def test_capability_doc_cors_header_present(self, client):
        r = client.get(
            "/.well-known/open-ranking.json",
            headers={"Origin": "https://nostr.example"},
        )
        assert r.headers.get("access-control-allow-origin") == "*"

    def test_options_preflight_on_stats(self, client):
        r = client.options(
            "/stats/pubkey",
            headers={
                "Origin": "https://nostr.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        # Per ORE-00, providers MUST handle OPTIONS preflight with 200.
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "*"


# ===========================================================================
# ORE-02: /stats/pubkey
# ===========================================================================
def _fake_stats(influence: float | None = 0.42):
    from app.schemas.schemas import UserConnectionCounts
    from app.services.user_service import UserRankAndCounts

    return UserRankAndCounts(
        influence=influence,
        counts=UserConnectionCounts(
            followed_by=10, following=20, muted_by=1, muting=2, reported_by=3, reporting=4,
        ),
    )


class TestStatsPubkey:
    def test_happy_path_global_algorithm(self, client, auth_headers):
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.42)),
        ):
            r = client.post(
                "/stats/pubkey", json={"pubkey": VALID_PUBKEY_1}, headers=auth_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pubkey"] == VALID_PUBKEY_1
        assert body["rank"] == pytest.approx(0.42)
        assert body["follows"] == 20
        assert body["followers"] == 10
        assert body["mutes"] == 2
        assert body["muters"] == 1
        assert body["reports"] == 4
        assert body["reporters"] == 3
        assert isinstance(body["ttl"], int) and body["ttl"] > 0

    def test_null_influence_becomes_zero_rank(self, client, auth_headers):
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(None)),
        ):
            r = client.post(
                "/stats/pubkey", json={"pubkey": VALID_PUBKEY_1}, headers=auth_headers,
            )
        assert r.status_code == 200
        assert r.json()["rank"] == 0.0

    def test_invalid_pubkey_returns_422(self, client, auth_headers):
        r = client.post(
            "/stats/pubkey", json={"pubkey": INVALID_PUBKEY}, headers=auth_headers,
        )
        assert r.status_code == 422

    def test_pov_required_when_algorithm_is_personalized(self, client, auth_headers):
        # graperank-pov requires a pov pubkey per the capability doc.
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1, "algorithm": "graperank-pov"},
            headers=auth_headers,
        )
        assert r.status_code == 422
        assert "pov" in r.text.lower()

    def test_unsupported_algorithm_returns_422(self, client, auth_headers):
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1, "algorithm": "made-up-algo"},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_personalized_algorithm_uses_pov_as_observer(self, client, auth_headers):
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["pubkey"] = pubkey
            observed["observer"] = observer
            return _fake_stats(0.9)

        with pov_availability(), patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
                headers=auth_headers,
            )
        assert r.status_code == 200
        assert observed == {"pubkey": VALID_PUBKEY_1, "observer": POV_PUBKEY}

    def test_asks_for_no_verified_line(self, client, auth_headers):
        """ORE-02 returns a rank and raw counts — nothing the observer's
        verified line decides. So it takes the lean service call and passes no
        line at all: there is none to resolve (no preset read, no Postgres on
        this path) and none to get wrong. Surfacing verified / tier / flagged
        here means switching to `get_user_overview` and resolving the preset,
        which is a deliberate change, not a default."""
        observed = {}

        async def _capture(**kwargs):
            observed.update(kwargs)
            return _fake_stats(0.9)

        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey", json={"pubkey": VALID_PUBKEY_1}, headers=auth_headers,
            )

        assert r.status_code == 200, r.text
        assert set(observed) == {"pubkey", "observer"}


# ===========================================================================
# ORE-03: /rank/pubkeys
# ===========================================================================
class TestRankPubkeys:
    def test_happy_path_default_limit_and_sorting(self, client, auth_headers):
        async def fake_batch(session, pubkeys, observer):
            return {
                VALID_PUBKEY_1: 0.1,
                VALID_PUBKEY_2: 0.9,
                VALID_PUBKEY_3: None,  # unknown -> coerced to 0.0
            }

        with patch(
            "app.routers.open_ranking.rank.batch_influence_for_pubkeys",
            new=fake_batch,
        ), patch("app.routers.open_ranking.rank.neo4j_driver"):
            r = client.post(
                "/rank/pubkeys",
                json={"pubkeys": [VALID_PUBKEY_1, VALID_PUBKEY_2, VALID_PUBKEY_3]},
                headers=auth_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        results = body["results"]
        # Length = number of input pubkeys when no limit given.
        assert len(results) == 3
        # Sorted by rank DESC.
        ranks = [r["rank"] for r in results]
        assert ranks == sorted(ranks, reverse=True)
        # Unknown pubkey is present and ranked 0.
        unknown = next(x for x in results if x["pubkey"] == VALID_PUBKEY_3)
        assert unknown["rank"] == 0.0

    def test_limit_clamped_to_requested(self, client, auth_headers):
        async def fake_batch(session, pubkeys, observer):
            return {pk: 0.5 for pk in pubkeys}

        with patch(
            "app.routers.open_ranking.rank.batch_influence_for_pubkeys",
            new=fake_batch,
        ), patch("app.routers.open_ranking.rank.neo4j_driver"):
            r = client.post(
                "/rank/pubkeys",
                json={
                    "pubkeys": [VALID_PUBKEY_1, VALID_PUBKEY_2, VALID_PUBKEY_3],
                    "limit": 2,
                },
                headers=auth_headers,
            )
        assert r.status_code == 200
        assert len(r.json()["results"]) == 2

    def test_empty_pubkeys_returns_422(self, client, auth_headers):
        r = client.post("/rank/pubkeys", json={"pubkeys": []}, headers=auth_headers)
        assert r.status_code == 422

    def test_invalid_limit_returns_422(self, client, auth_headers):
        r = client.post(
            "/rank/pubkeys",
            json={"pubkeys": [VALID_PUBKEY_1], "limit": 0},
            headers=auth_headers,
        )
        assert r.status_code == 422

    def test_oversize_batch_returns_413(self, client, auth_headers):
        big = [f"{i:064x}" for i in range(1001)]
        r = client.post("/rank/pubkeys", json={"pubkeys": big}, headers=auth_headers)
        assert r.status_code == 413

    def test_bad_pubkey_in_list_returns_422(self, client, auth_headers):
        r = client.post(
            "/rank/pubkeys",
            json={"pubkeys": [VALID_PUBKEY_1, INVALID_PUBKEY]},
            headers=auth_headers,
        )
        assert r.status_code == 422


# ===========================================================================
# ORE-05: /search/pubkeys
# ===========================================================================
class TestSearchPubkeys:
    def test_happy_path(self, client, auth_headers):
        seen_kwargs = {}

        async def fake_search(
            query_text,
            user_pubkey,
            hits,
            include_zero_score_results,
            *,
            ranking_profile=None,
            min_rank=None,
        ):
            seen_kwargs["ranking_profile"] = ranking_profile
            seen_kwargs["min_rank"] = min_rank
            seen_kwargs["include_zero_score_results"] = include_zero_score_results
            # _quality_score values deliberately contradict the relevance
            # order: rank MUST come from _relevance (the ordering key ORE-05
            # requires), not from the 0-100 trust score.
            return [
                {"pubkey": VALID_PUBKEY_1, "_relevance": 11988.6, "_quality_score": 0.0},
                {"pubkey": VALID_PUBKEY_2, "_relevance": 3018.9, "_quality_score": 61.0},
            ]

        with patch("app.routers.open_ranking.search.vespa_search", new=fake_search):
            r = client.post(
                "/search/pubkeys", json={"query": "jack"}, headers=auth_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert [x["pubkey"] for x in body["results"]] == [VALID_PUBKEY_1, VALID_PUBKEY_2]
        ranks = [x["rank"] for x in body["results"]]
        assert ranks == pytest.approx([11988.6, 3018.9])
        # ORE-05: "sorted by rank in descending order"
        assert ranks == sorted(ranks, reverse=True)
        # These arguments must mirror /search/byText's defaults (UI parity):
        # min_rank omitted degrades ordering to pure text (schema default
        # -1e9), and min_rank=0 / include_zero=True floods results with
        # zero-trust exact-name matches at multiplier 1.0.
        from app.core.vespa import DEFAULT_MIN_RANK, RANK_PROFILE_SORT_FOLLOWERS

        assert seen_kwargs["ranking_profile"] == RANK_PROFILE_SORT_FOLLOWERS
        assert seen_kwargs["min_rank"] == DEFAULT_MIN_RANK
        assert seen_kwargs["include_zero_score_results"] is False

    def test_empty_query_returns_422(self, client, auth_headers):
        r = client.post("/search/pubkeys", json={"query": ""}, headers=auth_headers)
        assert r.status_code == 422

    def test_query_too_long_returns_422(self, client, auth_headers):
        r = client.post(
            "/search/pubkeys", json={"query": "x" * 513}, headers=auth_headers,
        )
        assert r.status_code == 422


# ===========================================================================
# ORE-06 / ORE-07: /followers and /muters
# ===========================================================================
def _fake_top_inbound():
    from app.schemas.schemas import UserConnection

    return [
        UserConnection(pubkey=VALID_PUBKEY_2, influence=0.91),
        UserConnection(pubkey=VALID_PUBKEY_3, influence=0.50),
    ]


class TestFollowersMuters:
    def test_followers_happy_path(self, client, auth_headers):
        with patch(
            "app.routers.open_ranking.followers_muters.get_top_inbound_by_influence",
            new=AsyncMock(return_value=_fake_top_inbound()),
        ), patch(
            "app.routers.open_ranking.followers_muters.neo4j_driver"
        ), patch(
            "app.routers.open_ranking.followers_muters._redis_inbound_count",
            new=AsyncMock(return_value=1234),
        ):
            r = client.post(
                "/followers", json={"pubkey": VALID_PUBKEY_1}, headers=auth_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1234
        assert [x["pubkey"] for x in body["results"]] == [VALID_PUBKEY_2, VALID_PUBKEY_3]
        assert body["results"][0]["rank"] == pytest.approx(0.91)

    def test_muters_happy_path(self, client, auth_headers):
        with patch(
            "app.routers.open_ranking.followers_muters.get_top_inbound_by_influence",
            new=AsyncMock(return_value=_fake_top_inbound()),
        ), patch(
            "app.routers.open_ranking.followers_muters.neo4j_driver"
        ), patch(
            "app.routers.open_ranking.followers_muters._redis_inbound_count",
            new=AsyncMock(return_value=42),
        ):
            r = client.post(
                "/muters", json={"pubkey": VALID_PUBKEY_1}, headers=auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 42

    def test_invalid_pubkey_returns_422(self, client, auth_headers):
        r = client.post(
            "/followers", json={"pubkey": INVALID_PUBKEY}, headers=auth_headers,
        )
        assert r.status_code == 422

    def test_invalid_limit_returns_422(self, client, auth_headers):
        r = client.post(
            "/followers",
            json={"pubkey": VALID_PUBKEY_1, "limit": -5},
            headers=auth_headers,
        )
        assert r.status_code == 422


# ===========================================================================
# ORE-A: NWT authentication
# ===========================================================================
class TestNWTAuth:
    """NWT enforcement when `settings.open_ranking_require_auth` is True. The
    expected `aud` is derived from settings.public_base_url (set to
    http://localhost:8000 in the test env, so the audience is `localhost` —
    see tests.conftest.TEST_AUD).

    The provider default is OPEN (auth off); this class flips the flag on for
    its lifetime via the autouse fixture below.
    """

    @pytest.fixture(autouse=True)
    def _enable_auth(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "open_ranking_require_auth", True)

    def test_missing_authorization_returns_401(self, client):
        r = client.post("/stats/pubkey", json={"pubkey": VALID_PUBKEY_1})
        assert r.status_code == 401
        assert r.headers.get("x-reason")

    def test_valid_nwt_lets_request_through(self, client):
        token, _pk = make_nwt()  # aud defaults to TEST_AUD
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.5)),
        ):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text

    def test_wrong_audience_rejected(self, client):
        token, _pk = make_nwt(aud="other-provider.example")
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "aud" in r.headers.get("x-reason", "").lower()

    def test_expired_token_rejected(self, client):
        # Well outside the 60s skew window so the rejection is unambiguous.
        token, _pk = make_nwt(exp=int(time.time()) - 600)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "exp" in r.headers.get("x-reason", "").lower()

    def test_wrong_kind_rejected(self, client):
        token, _pk = make_nwt(kind=1)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 401

    def test_garbage_token_rejected(self, client):
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": "Nostr not-base64!@#"},
        )
        assert r.status_code == 401

    def test_capability_doc_remains_public(self, client):
        # ORE-01: discovery MUST work without auth.
        r = client.get("/.well-known/open-ranking.json")
        assert r.status_code == 200

    def test_multi_aud_token_accepted_when_one_matches(self, client):
        from tests.conftest import TEST_AUD

        token, _pk = make_nwt(
            aud=["other-provider.example", TEST_AUD, "third.example"]
        )
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.5)),
        ):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text

    def test_token_without_aud_rejected(self, client):
        token, _pk = make_nwt(aud=None)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "aud" in r.headers.get("x-reason", "").lower()

    def test_nbf_in_future_rejected(self, client):
        # nbf 10 minutes ahead — well outside the 60s clock-skew tolerance.
        future_nbf = int(time.time()) + 600
        token, _pk = make_nwt(nbf=future_nbf)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "nbf" in r.headers.get("x-reason", "").lower()

    def test_exp_within_skew_tolerated(self, client):
        # Token expired 5 seconds ago — inside the 60s allowed skew, so still
        # accepted. Defends against tight clock drift between client/server.
        token, _pk = make_nwt(exp=int(time.time()) - 5)
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.5)),
        ):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text


# Default observer = PERIODIC_GRAPERANK_PUBKEY in the test env (conftest).
DEFAULT_OBSERVER = "be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a"


# ===========================================================================
# Open mode (settings.open_ranking_require_auth is False — the provider default)
# ===========================================================================
class TestOpenModeNoAuth:
    """With auth disabled (the default), data endpoints are reachable without
    any NWT and resolve the observer per ORE-01 (global -> default observer,
    personalized -> client `pov`).
    """

    def test_request_without_token_succeeds(self, client):
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.5)),
        ):
            r = client.post("/stats/pubkey", json={"pubkey": VALID_PUBKEY_1})
        assert r.status_code == 200, r.text

    def test_open_mode_uses_default_observer_for_global_algorithm(self, client):
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.5)

        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post("/stats/pubkey", json={"pubkey": VALID_PUBKEY_1})
        assert r.status_code == 200, r.text
        assert observed["observer"] == DEFAULT_OBSERVER

    def test_open_mode_honours_client_pov(self, client):
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.5)

        with pov_availability(), patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 200, r.text
        assert observed["observer"] == POV_PUBKEY


# ===========================================================================
# Auth mode: the observer is ALWAYS the signer (own-scores-only guarantee)
# ===========================================================================
class TestAuthModeForcesOwnObserver:
    """When auth is enabled, the signer's own pubkey is forced as the observer
    for every query — a client-supplied `pov` cannot be used to read another
    observer's scores.
    """

    @pytest.fixture(autouse=True)
    def _enable_auth(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "open_ranking_require_auth", True)

    def test_pov_is_ignored_and_observer_is_signer(self, client):
        token, signer = make_nwt()
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.5)

        with pov_availability(), patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text
        # pov (POV_PUBKEY) is ignored — the signer is the observer.
        assert observed["observer"] == signer

    def test_global_algorithm_also_forced_to_signer(self, client):
        token, signer = make_nwt()
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.5)

        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},  # default (global) algorithm
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text
        assert observed["observer"] == signer

    def test_pov_algorithm_without_pov_is_accepted(self, client):
        # In auth mode the pov requirement is moot — the signer is the observer.
        token, signer = make_nwt()
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.5)

        with pov_availability(), patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1, "algorithm": "graperank-pov"},
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text
        assert observed["observer"] == signer

    def test_personalized_algo_gates_on_unprovisioned_signer(self, client):
        # The never-substitute contract applies to the effective observer, so
        # an authenticated signer with no scores gets the same honest 422.
        token, _signer = make_nwt()
        with pov_availability(exists=False):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1, "algorithm": "graperank-pov"},
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 422
        assert r.headers.get("x-reason")


# ===========================================================================
# ORE-01 §"Unavailable pov" (protocol PR #9): 422 / 202, never substitute
# ===========================================================================

# (endpoint, personalized algorithm id, minimal valid request body)
_PERSONALIZED_CASES = [
    ("/stats/pubkey", "graperank-pov", {"pubkey": VALID_PUBKEY_1}),
    ("/rank/pubkeys", "graperank-pov", {"pubkeys": [VALID_PUBKEY_1]}),
    ("/search/pubkeys", "relevance-pov", {"query": "jack"}),
    ("/followers", "graperank-pov", {"pubkey": VALID_PUBKEY_1}),
    ("/muters", "graperank-pov", {"pubkey": VALID_PUBKEY_1}),
]

# The default (global) algorithm id the 422 reason must offer as fallback.
_DEFAULT_ALGO = {
    "/stats/pubkey": "graperank",
    "/rank/pubkeys": "graperank",
    "/search/pubkeys": "relevance",
    "/followers": "graperank",
    "/muters": "graperank",
}


class TestPovAvailability:
    """A personalized algorithm with a pov the provider cannot serve MUST
    return 422 with an X-Reason naming an explicit fallback, MUST NOT be
    answered from a substituted point of view, and a provisioned pov whose
    scores are still computing gets 202 + Retry-After."""

    @pytest.mark.parametrize("endpoint,algo,body", _PERSONALIZED_CASES)
    def test_unknown_pov_returns_422_on_every_endpoint(
        self, client, endpoint, algo, body
    ):
        with pov_availability(exists=False):
            r = client.post(
                endpoint, json={**body, "algorithm": algo, "pov": POV_PUBKEY}
            )
        assert r.status_code == 422, r.text
        reason = r.headers.get("x-reason", "")
        assert reason, "422 must carry X-Reason"
        # The reason must offer the explicit alternative: the endpoint's
        # default global algorithm (ORE-01: first element of the array).
        assert _DEFAULT_ALGO[endpoint] in reason
        assert "fall back" in reason
        assert r.json()["error"] == reason

    def test_unavailable_pov_never_reaches_the_data_source(self, client):
        """Never-substitute invariant: the refusal happens before any ranking
        data is read, so no substituted or zero-filled result can leak out."""
        service = AsyncMock(return_value=_fake_stats(0.9))
        with pov_availability(exists=False), patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=service
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 422
        service.assert_not_awaited()

    def test_house_pov_is_always_available(self, client):
        """The default observer IS the global perspective — usable as an
        explicit pov without any provisioning row. No availability stubs on
        purpose: if the gate ever consults the DB for the house pov, the
        sentinel session explodes and this test fails."""
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.9)

        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": DEFAULT_OBSERVER,
                },
            )
        assert r.status_code == 200, r.text
        assert observed["observer"] == DEFAULT_OBSERVER

    def test_global_algorithm_is_never_gated(self, client):
        """A pov sent to a global algorithm MUST be ignored (ORE-01) — and the
        availability gate must not even run (no stubs, same sentinel logic as
        above)."""
        observed = {}

        async def _capture(*, pubkey, observer, **_):
            observed["observer"] = observer
            return _fake_stats(0.5)

        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts", new=_capture
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 200, r.text
        assert observed["observer"] == DEFAULT_OBSERVER

    def test_provisioned_and_ready_pov_returns_200(self, client):
        with pov_availability(exists=True, graperank=True), patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.7)),
        ):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 200, r.text
        assert r.json()["rank"] == pytest.approx(0.7)

    def test_provisioned_computing_pov_returns_202_with_retry_after(self, client):
        with pov_availability(exists=True, graperank=False, in_pipeline=True):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 202, r.text
        assert r.headers.get("retry-after", "").isdigit()
        assert r.headers.get("x-reason")
        body = r.json()
        assert body["status"] == "computing"
        assert body["retry_after"] == int(r.headers["retry-after"])

    def test_provisioned_stalled_pov_returns_422(self, client):
        with pov_availability(exists=True, graperank=False, in_pipeline=False):
            r = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 422
        assert r.headers.get("x-reason")

    def test_search_availability_is_independent_of_graperank(self, client):
        """GrapeRank done but the Vespa mirror never landed: stats serves,
        search refuses — availability is per data source."""
        avail = pov_availability(
            exists=True, graperank=True, search=False, in_pipeline=False
        )
        with avail, patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.7)),
        ):
            stats = client.post(
                "/stats/pubkey",
                json={
                    "pubkey": VALID_PUBKEY_1,
                    "algorithm": "graperank-pov",
                    "pov": POV_PUBKEY,
                },
            )
            search = client.post(
                "/search/pubkeys",
                json={
                    "query": "jack",
                    "algorithm": "relevance-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert stats.status_code == 200
        assert search.status_code == 422
        assert "search index" in search.headers.get("x-reason", "")

    def test_search_mirror_in_flight_returns_202(self, client):
        with pov_availability(
            exists=True, graperank=True, search=False, in_pipeline=True
        ):
            r = client.post(
                "/search/pubkeys",
                json={
                    "query": "jack",
                    "algorithm": "relevance-pov",
                    "pov": POV_PUBKEY,
                },
            )
        assert r.status_code == 202
        assert r.headers.get("retry-after")


# ===========================================================================
# ORE-00 §Errors: X-Reason + {"error": ...} on every ORE error response
# ===========================================================================
class TestOREErrorShape:
    def test_invalid_pubkey_carries_x_reason_and_error_body(self, client):
        r = client.post("/stats/pubkey", json={"pubkey": INVALID_PUBKEY})
        assert r.status_code == 422
        reason = r.headers.get("x-reason", "")
        assert "pubkey" in reason
        assert r.json() == {"error": reason}

    def test_unsupported_algorithm_carries_x_reason(self, client):
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1, "algorithm": "made-up-algo"},
        )
        assert r.status_code == 422
        assert "made-up-algo" in r.headers.get("x-reason", "")

    def test_missing_pov_carries_x_reason(self, client):
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1, "algorithm": "graperank-pov"},
        )
        assert r.status_code == 422
        assert "pov" in r.headers.get("x-reason", "")

    def test_oversize_batch_413_carries_x_reason(self, client):
        big = [f"{i:064x}" for i in range(1001)]
        r = client.post("/rank/pubkeys", json={"pubkeys": big})
        assert r.status_code == 413
        assert r.headers.get("x-reason")
        assert "error" in r.json()

    def test_method_not_allowed_carries_x_reason(self, client):
        r = client.get("/stats/pubkey")
        assert r.status_code == 405
        assert r.headers.get("x-reason")

    def test_missing_required_field_carries_x_reason(self, client):
        r = client.post("/stats/pubkey", json={})
        assert r.status_code == 422
        reason = r.headers.get("x-reason", "")
        assert "pubkey" in reason
        assert r.json()["error"] == reason

    def test_malformed_json_returns_400_with_x_reason(self, client):
        r = client.post(
            "/stats/pubkey",
            content="{not json",
            headers={"Content-Type": "application/json"},
        )
        # ORE-02..07 error tables: 400 = malformed request body.
        assert r.status_code == 400
        assert r.headers.get("x-reason")

    def test_error_headers_are_cors_exposed(self, client):
        """Browser clients must be able to read X-Reason / Retry-After
        cross-origin, or the SHOULD-explain contract is dead letter on the
        web."""
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": INVALID_PUBKEY},
            headers={"Origin": "https://nostr.example"},
        )
        assert r.status_code == 422
        exposed = r.headers.get("access-control-expose-headers", "")
        assert "X-Reason" in exposed
        assert "Retry-After" in exposed

    def test_non_ore_paths_keep_default_error_shape(self, client):
        """The handlers self-scope to ORE paths; everything else keeps
        FastAPI's {"detail": ...} shape with no X-Reason."""
        r = client.get("/definitely-not-an-ore-path")
        assert r.status_code == 404
        assert "detail" in r.json()
        assert r.headers.get("x-reason") is None


# ===========================================================================
# OpenAPI / Swagger: the generated schema documents the ORE error + retry
# contract the handlers actually implement
# ===========================================================================
class TestOpenAPISchema:
    DATA_PATHS = [
        "/stats/pubkey",
        "/rank/pubkeys",
        "/search/pubkeys",
        "/followers",
        "/muters",
    ]

    @pytest.fixture()
    def openapi(self, client) -> dict:
        r = client.get("/openapi.json")
        assert r.status_code == 200
        return r.json()

    def test_data_endpoints_document_the_pov_contract(self, openapi):
        """Every data endpoint documents 202 (pov computing) with the
        Retry-After header and the {"status", "retry_after"} body, plus the
        error statuses the ORE handlers can emit."""
        for path in self.DATA_PATHS:
            responses = openapi["paths"][path]["post"]["responses"]
            for code in ("202", "400", "401", "403", "422"):
                assert code in responses, f"{path} does not document {code}"
            r202 = responses["202"]
            schema_ref = r202["content"]["application/json"]["schema"]["$ref"]
            assert schema_ref.endswith("OreComputingResponse")
            assert "Retry-After" in r202["headers"]
            assert "X-Reason" in r202["headers"]

    def test_error_responses_document_the_ore_error_shape(self, openapi):
        """Every documented error is {"error": ...} + X-Reason — NOT FastAPI's
        auto-generated 422 HTTPValidationError ({"detail": [...]}), which the
        ORE exception handlers never emit on these paths."""
        for path in self.DATA_PATHS:
            responses = openapi["paths"][path]["post"]["responses"]
            for code, resp in responses.items():
                if code in ("200", "202"):
                    continue
                schema_ref = resp["content"]["application/json"]["schema"]["$ref"]
                assert schema_ref.endswith(
                    "OreErrorResponse"
                ), f"{path} {code} documents {schema_ref}"
                assert (
                    "X-Reason" in resp["headers"]
                ), f"{path} {code} does not document the X-Reason header"

    def test_batch_endpoint_documents_413(self, openapi):
        responses = openapi["paths"]["/rank/pubkeys"]["post"]["responses"]
        assert "413" in responses
        assert "1000" in responses["413"]["description"]
