"""End-to-end tests for the Open Ranking endpoints.

These boot a FastAPI app, route a real HTTP request through ASGI, and verify
the response per the ORE specs. The Neo4j / Redis / search-relay boundaries are
patched at import sites in the open_ranking router modules so the suite is
deterministic and infra-free.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nostr_sdk import Keys

from app.utils.observer import default_observer_pubkey
from tests.conftest import make_nwt

VALID_PUBKEY_1 = "a" * 64
VALID_PUBKEY_2 = "b" * 64
VALID_PUBKEY_3 = "c" * 64
POV_PUBKEY = "d" * 64
INVALID_PUBKEY = "not-a-valid-pubkey"
HOUSE = default_observer_pubkey()


@contextmanager
def pov_availability(
    exists: bool = True,
    graperank: bool = True,
    in_pipeline: bool = False,
):
    """Stub the availability gate's repo reads (patched at their import site
    in the availability module). Defaults describe a fully provisioned,
    fully computed pov."""
    with patch(
        "app.routers.open_ranking.availability.get_pov_availability_fields_on_db",
        new=AsyncMock(return_value=(exists, graperank)),
    ), patch(
        "app.routers.open_ranking.availability.any_request_in_pipeline_for_pubkey_on_db",
        new=AsyncMock(return_value=in_pipeline),
    ):
        yield


class FakeRelay:
    """Stands in for the search relay's private `POST /search/pubkeys`."""

    def __init__(self, answer: httpx.Response | Exception | None = None):
        self.answer = (
            answer
            if answer is not None
            else httpx.Response(200, json={"results": [], "ttl": 300})
        )
        self.request: httpx.Request | None = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def __enter__(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url="http://relay.test"
        )
        self._patch = patch("app.core.search_relay._client", client)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


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
            "/recommend/pubkeys",
        ):
            assert path in body, f"capability doc missing {path}"
            assert isinstance(body[path], list) and body[path]
            for algo in body[path]:
                assert "id" in algo

    def test_search_algorithms_describe_search_relevance(self, client):
        search = client.get("/.well-known/open-ranking.json").json()["/search/pubkeys"]
        assert [a["id"] for a in search] == ["relevance", "relevance-pov"]
        for algo in search:
            assert algo["description"].startswith(
                "rank = search relevance: kind-0 text match weighted by "
                "GrapeRank trust; query-relative"
            )

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
            followed_by=10,
            following=20,
            muted_by=1,
            muting=2,
            reported_by=3,
            reporting=4,
        ),
    )


class TestStatsPubkey:
    def test_happy_path_global_algorithm(self, client, auth_headers):
        with patch(
            "app.routers.open_ranking.stats.get_user_rank_and_counts",
            new=AsyncMock(return_value=_fake_stats(0.42)),
        ):
            r = client.post(
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},
                headers=auth_headers,
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
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},
                headers=auth_headers,
            )
        assert r.status_code == 200
        assert r.json()["rank"] == 0.0

    def test_invalid_pubkey_returns_422(self, client, auth_headers):
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": INVALID_PUBKEY},
            headers=auth_headers,
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
                "/stats/pubkey",
                json={"pubkey": VALID_PUBKEY_1},
                headers=auth_headers,
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
    """ORE-05 is served by the search relay; this endpoint only forwards."""

    def test_forwards_the_body_and_returns_the_relays_200_verbatim(self, client):
        body = {
            "results": [
                {"pubkey": VALID_PUBKEY_1, "rank": 11988.6},
                {"pubkey": VALID_PUBKEY_2, "rank": 3018.9},
            ],
            "ttl": 300,
        }
        sent = {"query": "jack", "algorithm": "relevance-pov", "pov": POV_PUBKEY}
        with FakeRelay(httpx.Response(200, json=body)) as relay:
            r = client.post("/search/pubkeys", json=sent)
        assert r.status_code == 200, r.text
        assert r.json() == body
        assert relay.request.method == "POST"
        assert relay.request.url.path == "/search/pubkeys"
        assert json.loads(relay.request.content) == {**sent, "observer": HOUSE}

    def test_names_our_house_observer_over_any_the_client_sent(self, client):
        sent = {"query": "jack", "observer": VALID_PUBKEY_2}
        with FakeRelay() as relay:
            r = client.post("/search/pubkeys", json=sent)
        assert r.status_code == 200, r.text
        assert json.loads(relay.request.content) == {"query": "jack", "observer": HOUSE}

    def test_a_body_that_is_not_an_object_goes_as_is(self, client):
        with FakeRelay(httpx.Response(400, json={"error": "malformed"})) as relay:
            r = client.post(
                "/search/pubkeys",
                content=b"[1]",
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 400
        assert relay.request.content == b"[1]"

    @pytest.mark.parametrize(
        "status,body,headers",
        [
            (
                202,
                {"status": "computing"},
                {"X-Reason": "relay still mirroring", "Retry-After": "60"},
            ),
            (422, {"error": "no trust provider list"}, {"X-Reason": "no list"}),
            (400, {"error": "malformed"}, {"X-Reason": "malformed"}),
            (503, {"error": "busy"}, {"X-Reason": "busy", "Retry-After": "5"}),
        ],
    )
    def test_relay_status_body_and_reason_pass_through(
        self, client, status, body, headers
    ):
        with FakeRelay(httpx.Response(status, json=body, headers=headers)):
            r = client.post("/search/pubkeys", json={"query": "jack"})
        assert r.status_code == status
        assert r.json() == body
        for name, value in headers.items():
            assert r.headers[name] == value

    @pytest.mark.parametrize(
        "answer",
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.Response(500, text="boom"),
            httpx.Response(404, text="ORE disabled"),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"hits": []}),
            httpx.Response(200, json={"results": [{"pubkey": VALID_PUBKEY_1}]}),
            httpx.Response(
                200, json={"results": [{"pubkey": VALID_PUBKEY_1, "rank": "high"}]}
            ),
            httpx.Response(202, json={"status": "computing"}),
        ],
    )
    def test_relay_down_or_off_spec_returns_503_with_reason(self, client, answer):
        with FakeRelay(answer):
            r = client.post("/search/pubkeys", json={"query": "jack"})
        assert r.status_code == 503, r.text
        assert "search relay" in r.headers["x-reason"]
        assert r.json()["error"] == r.headers["x-reason"]

    @pytest.mark.parametrize("sent_pov", [POV_PUBKEY, None])
    def test_auth_forces_the_signer_as_pov(self, client, monkeypatch, sent_pov):
        from app.core.config import settings

        monkeypatch.setattr(settings, "open_ranking_require_auth", True)
        token, signer = make_nwt()
        sent = {"query": "jack", "algorithm": "relevance-pov", "limit": 5}
        if sent_pov:
            sent["pov"] = sent_pov
        with FakeRelay() as relay:
            r = client.post(
                "/search/pubkeys",
                json=sent,
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text
        assert json.loads(relay.request.content) == {
            **sent,
            "pov": signer,
            "observer": HOUSE,
        }

    def test_auth_global_relevance_forwards_signer_and_passes_house_view_through(
        self, client, monkeypatch
    ):
        # ORE-01: the relay ignores pov for global `relevance` and ranks through
        # the observer we name, so the House view comes back even with the signer
        # forced in.
        from app.core.config import settings

        monkeypatch.setattr(settings, "open_ranking_require_auth", True)
        token, signer = make_nwt()
        house_view = {"results": [{"pubkey": VALID_PUBKEY_1, "rank": 42.0}], "ttl": 300}
        sent = {"query": "jack", "algorithm": "relevance"}
        with FakeRelay(httpx.Response(200, json=house_view)) as relay:
            r = client.post(
                "/search/pubkeys",
                json=sent,
                headers={"Authorization": f"Nostr {token}"},
            )
        assert r.status_code == 200, r.text
        assert json.loads(relay.request.content) == {
            **sent,
            "pov": signer,
            "observer": HOUSE,
        }
        assert r.json() == house_view

    def test_auth_on_without_token_never_reaches_the_relay(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "open_ranking_require_auth", True)
        with FakeRelay() as relay:
            r = client.post("/search/pubkeys", json={"query": "jack"})
        assert r.status_code == 401
        assert relay.request is None


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
        ), patch("app.routers.open_ranking.followers_muters.neo4j_driver"), patch(
            "app.routers.open_ranking.followers_muters._redis_inbound_count",
            new=AsyncMock(return_value=1234),
        ):
            r = client.post(
                "/followers",
                json={"pubkey": VALID_PUBKEY_1},
                headers=auth_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1234
        assert [x["pubkey"] for x in body["results"]] == [
            VALID_PUBKEY_2,
            VALID_PUBKEY_3,
        ]
        assert body["results"][0]["rank"] == pytest.approx(0.91)

    def test_muters_happy_path(self, client, auth_headers):
        with patch(
            "app.routers.open_ranking.followers_muters.get_top_inbound_by_influence",
            new=AsyncMock(return_value=_fake_top_inbound()),
        ), patch("app.routers.open_ranking.followers_muters.neo4j_driver"), patch(
            "app.routers.open_ranking.followers_muters._redis_inbound_count",
            new=AsyncMock(return_value=42),
        ):
            r = client.post(
                "/muters",
                json={"pubkey": VALID_PUBKEY_1},
                headers=auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 42

    def test_invalid_pubkey_returns_422(self, client, auth_headers):
        r = client.post(
            "/followers",
            json={"pubkey": INVALID_PUBKEY},
            headers=auth_headers,
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

        token, _pk = make_nwt(aud=["other-provider.example", TEST_AUD, "third.example"])
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


    def test_token_without_exp_rejected(self, client):
        token, _pk = make_nwt(exp=None)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "exp" in r.headers.get("x-reason", "").lower()


    def test_lifetime_over_cap_rejected(self, client):
        now = int(time.time())
        token, _pk = make_nwt(iat=now, exp=now + 3600 + 1)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "lifetime" in r.headers.get("x-reason", "").lower()

    def test_lifetime_over_cap_measured_from_created_at_without_iat(self, client):
        token, _pk = make_nwt(exp=int(time.time()) + 2 * 3600)
        r = client.post(
            "/stats/pubkey",
            json={"pubkey": VALID_PUBKEY_1},
            headers={"Authorization": f"Nostr {token}"},
        )
        assert r.status_code == 403
        assert "lifetime" in r.headers.get("x-reason", "").lower()

    def test_lifetime_within_configured_cap_accepted(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "open_ranking_nwt_max_lifetime_seconds", 7200)
        now = int(time.time())
        token, _pk = make_nwt(iat=now, exp=now + 7200)
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
    ("/followers", "graperank-pov", {"pubkey": VALID_PUBKEY_1}),
    ("/muters", "graperank-pov", {"pubkey": VALID_PUBKEY_1}),
    ("/recommend/pubkeys", "merchants-pov", {}),
]

# The default (global) algorithm id the 422 reason must offer as fallback.
_DEFAULT_ALGO = {
    "/stats/pubkey": "graperank",
    "/rank/pubkeys": "graperank",
    "/followers": "graperank",
    "/muters": "graperank",
    "/recommend/pubkeys": "merchants",
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
