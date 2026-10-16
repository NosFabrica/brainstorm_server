"""ORE-04 POST /recommend/pubkeys: merchants a point of view trusts.

The search relay is stubbed at its NIP-FE boundary (a REQ frame POSTed to `/`,
answered as ndjson) and Neo4j at the recommend module's import sites, like the
other Open Ranking suites.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.utils.observer import default_observer_pubkey
from tests.open_ranking.test_open_ranking_e2e import pov_availability

SELLER_A = "a" * 64
SELLER_B = "b" * 64
SELLER_C = "c" * 64
POV = "d" * 64
HOUSE = default_observer_pubkey()


def _listing(author: str, d: str) -> dict:
    return {"id": d.ljust(64, "0"), "pubkey": author, "kind": 30402, "tags": [["d", d]]}


def _ndjson(*frames: list) -> str:
    return "\n".join(json.dumps(f) for f in frames) + "\n"


class FakeRelay:
    """Answers one NIP-FE REQ; records the frame it was sent."""

    def __init__(
        self,
        body: str | None = None,
        status_code: int = 200,
        error: Exception | None = None,
    ):
        self.body = body if body is not None else _ndjson(["EOSE", "ore"])
        self.status_code = status_code
        self.error = error
        self.headers: dict[str, str] = {}
        self.frame: list | None = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"
        self.frame = json.loads(request.content)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_code, text=self.body, headers=self.headers)

    def __enter__(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url="http://relay.test"
        )
        self._patch = patch("app.core.search_relay._client", client)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _influence(ranks: dict[str, float | None]):
    async def fake(session, pubkeys, observer):
        fake.observer = observer
        fake.asked = list(pubkeys)
        return {pk: ranks.get(pk) for pk in pubkeys}

    return fake


@pytest.fixture(autouse=True)
def fresh_cache():
    from app.routers.open_ranking import recommend

    recommend._cache.clear()
    yield
    recommend._cache.clear()


@pytest.fixture()
def neo4j():
    with patch("app.routers.open_ranking.recommend.neo4j_driver"):
        yield


def _post(client, **body):
    return client.post("/recommend/pubkeys", json=body)


class TestRecommendMerchants:
    def test_merchants_are_distinct_authors_ranked_by_influence(self, client, neo4j):
        relay_body = _ndjson(
            ["EVENT", "ore", _listing(SELLER_A, "a1")],
            ["EVENT", "ore", _listing(SELLER_A, "a2")],
            ["EVENT", "ore", _listing(SELLER_B, "b1")],
            ["EVENT", "ore", _listing(SELLER_C, "c1")],
            ["EOSE", "ore"],
        )
        fake = _influence({SELLER_A: 0.4, SELLER_B: 0.9, SELLER_C: 0.1})
        with FakeRelay(relay_body), patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys", new=fake
        ):
            r = _post(client)

        assert r.status_code == 200, r.text
        body = r.json()
        assert [x["pubkey"] for x in body["results"]] == [SELLER_B, SELLER_A, SELLER_C]
        assert body["results"][0]["rank"] == pytest.approx(0.9)
        assert isinstance(body["ttl"], int) and body["ttl"] > 0
        assert sorted(fake.asked) == sorted([SELLER_A, SELLER_B, SELLER_C])

    def test_global_algorithm_reads_listings_through_the_house_lens(
        self, client, neo4j
    ):
        fake = _influence({})
        with FakeRelay() as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys", new=fake
        ):
            r = _post(client, algorithm="merchants", pov=POV)

        assert r.status_code == 200, r.text
        verb, _sub, nostr_filter = relay.frame
        assert verb == "REQ"
        assert nostr_filter["kinds"] == [30402]
        assert nostr_filter["search"] == f"sort:rank observer:{HOUSE}"
        assert fake.observer == HOUSE

    def test_personalized_algorithm_ranks_through_the_pov(self, client, neo4j):
        fake = _influence({SELLER_A: 0.7})
        body = _ndjson(["EVENT", "ore", _listing(SELLER_A, "a1")], ["EOSE", "ore"])
        with pov_availability(), FakeRelay(body) as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys", new=fake
        ):
            r = _post(client, algorithm="merchants-pov", pov=POV)

        assert r.status_code == 200, r.text
        assert relay.frame[2]["search"] == f"sort:rank observer:{POV}"
        assert fake.observer == POV

    def test_topic_algorithm_narrows_the_listings_and_cannot_carry_extension_tokens(
        self, client, neo4j
    ):
        with FakeRelay() as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys",
            new=_influence({}),
        ):
            r = _post(
                client,
                algorithm="merchants-topic",
                topic='bitcoin "sneakers 10:30 https://x.io include:spam observer:'
                + "e" * 64,
            )

        assert r.status_code == 200, r.text
        assert relay.frame[2]["search"] == (
            f"bitcoin sneakers 10:30 https://x.io sort:rank observer:{HOUSE}"
        )

    def test_an_unclosed_quote_never_swallows_the_lens(self, client, neo4j):
        with FakeRelay() as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys",
            new=_influence({}),
        ):
            r = _post(client, algorithm="merchants-topic", topic='"vintage')
        assert r.status_code == 200, r.text
        assert '"' not in relay.frame[2]["search"]

    def test_a_topic_is_ignored_by_an_algorithm_that_does_not_declare_it(
        self, client, neo4j
    ):
        with FakeRelay() as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys",
            new=_influence({}),
        ):
            r = _post(client, algorithm="merchants", topic="x" * 600)
        assert r.status_code == 200, r.text
        assert relay.frame[2]["search"] == f"sort:rank observer:{HOUSE}"

    @pytest.mark.parametrize("topic", [None, "", "   ", "include:spam", '""'])
    def test_a_topic_algorithm_without_searchable_words_returns_422(
        self, client, topic
    ):
        body = {"algorithm": "merchants-topic"}
        if topic is not None:
            body["topic"] = topic
        r = client.post("/recommend/pubkeys", json=body)
        assert r.status_code == 422, r.text
        assert "topic" in r.headers.get("x-reason", "")

    def test_unscored_sellers_follow_the_scored_ones_in_the_relays_order(
        self, client, neo4j
    ):
        # The relay's lens already gated these authors; influence only orders
        # them, so a seller with no score is ranked last, never dropped.
        body = _ndjson(
            ["EVENT", "ore", _listing(SELLER_B, "b1")],
            ["EVENT", "ore", _listing(SELLER_A, "a1")],
            ["EVENT", "ore", _listing(SELLER_C, "c1")],
            ["EOSE", "ore"],
        )
        with FakeRelay(body), patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys",
            new=_influence({SELLER_A: None, SELLER_B: None, SELLER_C: 0.9}),
        ):
            r = _post(client)
        results = r.json()["results"]
        assert [x["pubkey"] for x in results] == [SELLER_C, SELLER_B, SELLER_A]
        assert [x["rank"] for x in results] == [pytest.approx(0.9), 0.0, 0.0]

    def test_a_pov_ranked_by_another_provider_still_gets_the_relays_sellers(
        self, client, neo4j
    ):
        # A pov whose 10040 names another provider: the relay gates by that
        # provider's cards, and our graph holds no influence for the pov at
        # all. The answer is the relay's sellers, in its order, not nothing.
        body = _ndjson(
            ["EVENT", "ore", _listing(SELLER_B, "b1")],
            ["EVENT", "ore", _listing(SELLER_A, "a1")],
            ["EVENT", "ore", _listing(SELLER_B, "b2")],
            ["EVENT", "ore", _listing(SELLER_C, "c1")],
            ["EOSE", "ore"],
        )
        fake = _influence({})
        with pov_availability(), FakeRelay(body) as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys", new=fake
        ):
            r = _post(client, algorithm="merchants-pov", pov=POV, limit=2)

        assert r.status_code == 200, r.text
        assert relay.frame[2]["search"] == f"sort:rank observer:{POV}"
        assert fake.observer == POV
        assert [x["pubkey"] for x in r.json()["results"]] == [SELLER_B, SELLER_A]

    def test_limit_caps_the_results_and_sizes_the_ask(self, client, neo4j):
        body = _ndjson(
            ["EVENT", "ore", _listing(SELLER_A, "a1")],
            ["EVENT", "ore", _listing(SELLER_B, "b1")],
            ["EVENT", "ore", _listing(SELLER_C, "c1")],
            ["EOSE", "ore"],
        )
        with FakeRelay(body) as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys",
            new=_influence({SELLER_A: 0.5, SELLER_B: 0.6, SELLER_C: 0.7}),
        ):
            r = _post(client, limit=2)
        assert [x["pubkey"] for x in r.json()["results"]] == [SELLER_C, SELLER_B]
        assert relay.frame[2]["limit"] == 10

    @pytest.mark.parametrize("limit", [0, -1, 1001])
    def test_bad_limit_returns_422(self, client, limit):
        assert _post(client, limit=limit).status_code == 422

    def test_overlong_topic_returns_422(self, client):
        assert (
            _post(client, algorithm="merchants-topic", topic="x" * 513).status_code
            == 422
        )

    def test_unknown_algorithm_returns_422(self, client):
        assert _post(client, algorithm="for-you").status_code == 422

    def test_personalized_without_pov_returns_422(self, client):
        r = _post(client, algorithm="merchants-pov")
        assert r.status_code == 422
        assert "pov" in r.text.lower()

    @pytest.mark.parametrize(
        "relay",
        [
            FakeRelay(_ndjson(["CLOSED", "ore", "auth-required: name an observer"])),
            FakeRelay(_ndjson(["NOTICE", "busy"])),
            FakeRelay(_ndjson(["EVENT", "ore", _listing(SELLER_A, "a1")])),  # no EOSE
            FakeRelay("not json\n"),
            FakeRelay(status_code=503),
            FakeRelay(error=httpx.ConnectError("refused")),
        ],
    )
    def test_a_relay_that_cannot_answer_is_a_503_not_an_empty_list(
        self, client, neo4j, relay
    ):
        with relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys",
            new=_influence({}),
        ):
            r = _post(client)
        assert r.status_code == 503, r.text
        assert r.headers.get("x-reason")

    def test_a_relays_retry_after_is_passed_on(self, client, neo4j):
        relay = FakeRelay(status_code=429)
        relay.headers = {"Retry-After": "2"}
        with relay:
            r = _post(client)
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "2"

    def test_an_answer_is_reused_until_it_expires(self, client, neo4j):
        body = _ndjson(["EVENT", "ore", _listing(SELLER_A, "a1")], ["EOSE", "ore"])
        fake = _influence({SELLER_A: 0.5})
        with FakeRelay(body) as relay, patch(
            "app.routers.open_ranking.recommend.batch_influence_for_pubkeys", new=fake
        ):
            first = _post(client)
            relay.frame = None
            second = _post(client)
        assert first.json() == second.json()
        assert relay.frame is None

    def test_capability_doc_advertises_the_four_algorithms(self, client):
        doc = client.get("/.well-known/open-ranking.json").json()
        algos = {a["id"]: a for a in doc["/recommend/pubkeys"]}
        assert list(algos) == [
            "merchants",
            "merchants-pov",
            "merchants-topic",
            "merchants-topic-pov",
        ]
        assert algos["merchants-topic"].get("topic") is True
        assert algos["merchants-topic-pov"].get("topic") is True
        assert not algos["merchants"].get("topic")
