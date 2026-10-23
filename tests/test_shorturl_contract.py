"""The shortener's HTTP contract: validation shape and wire format.

Validation belongs in the request schema, so bad input is a 422 from the
framework rather than a hand-rolled 400 in service code. Per-kind key rules are
covered in ``test_shorturl_entities.py``; this checks they surface as 422s.
"""

import pytest
from unittest.mock import AsyncMock

from app.api import app as fastapi_app
from app.core.database import get_db
from app.routers.shorturl.router import rate_limit_create_short_url
from app.schemas.schemas import CreatedShortUrl, ShortUrlEntity

PK_HEX = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"
PK_NPUB = "npub17ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqr5qjta"
PATH = f"/p/{PK_NPUB}"


def _body(**overrides):
    return {"kind": "profile", "key": PK_HEX, "relays": [], **overrides}


@pytest.fixture
def api(client, monkeypatch):
    """The shortener with storage mocked out — this is about the contract."""

    async def _no_db():
        yield None

    async def _no_rate_limit():
        return None

    # The 1 req/s limiter would 429 every test after the first, and would reach
    # a real Redis. It has its own coverage in test_rate_limiting.py.
    fastapi_app.dependency_overrides[get_db] = _no_db
    fastapi_app.dependency_overrides[rate_limit_create_short_url] = _no_rate_limit
    created = AsyncMock(
        return_value=CreatedShortUrl(
            short_code="AB3XK9QZ", kind="profile", key=PK_HEX, relays=[], path=PATH
        )
    )
    monkeypatch.setattr("app.routers.shorturl.router.create_short_url", created)
    yield client, created
    # no teardown needed: conftest's `client` fixture clears dependency_overrides


# --------------------------------------------------------------------------
# wire format — the frontend and og are built against it
# --------------------------------------------------------------------------


def test_a_mint_answers_short_code_kind_key_relays_and_path(api):
    client, _ = api
    body = client.post("/shorturl", json=_body()).json()
    assert body["data"] == {
        "shortCode": "AB3XK9QZ",
        "kind": "profile",
        "key": PK_HEX,
        "relays": [],
        "path": PATH,
    }


def test_a_resolve_answers_kind_key_relays_and_path(api, monkeypatch):
    client, _ = api
    entity = ShortUrlEntity(kind="profile", key=PK_HEX, relays=[], path=PATH)
    monkeypatch.setattr(
        "app.routers.shorturl.router.get_short_url_entity",
        AsyncMock(return_value=entity),
    )
    body = client.get("/shorturl/AB3XK9QZ").json()
    assert body["data"] == {
        "kind": "profile",
        "key": PK_HEX,
        "relays": [],
        "path": PATH,
    }


def test_the_old_pubkey_body_is_a_422(api):
    client, created = api
    r = client.post("/shorturl", json={"pubkey": PK_HEX, "relays": []})
    assert r.status_code == 422
    assert created.await_count == 0


# --------------------------------------------------------------------------
# kind and key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["url", "", "PROFILE", None])
def test_an_unknown_kind_is_a_422(api, kind):
    client, created = api
    assert client.post("/shorturl", json=_body(kind=kind)).status_code == 422
    assert created.await_count == 0


@pytest.mark.parametrize(
    "kind,key",
    [
        ("profile", "not-a-pubkey"),
        ("event", PK_NPUB),
        ("address", f"30023:{PK_HEX}"),
        ("hashtag", "two words"),
        ("tag", PK_HEX),
    ],
)
def test_a_key_that_does_not_fit_its_kind_is_a_422(api, kind, key):
    client, created = api
    assert client.post("/shorturl", json=_body(kind=kind, key=key)).status_code == 422
    assert created.await_count == 0, "a bad key must never reach storage"


def test_the_key_reaches_the_service_normalised(api):
    client, created = api
    assert client.post("/shorturl", json=_body(key=f"  {PK_NPUB} ")).status_code == 200
    assert created.await_args.args[1:3] == ("profile", PK_HEX)


def test_a_hashtag_reaches_the_service_normalised(api):
    client, created = api
    r = client.post("/shorturl", json=_body(kind="hashtag", key=" #Nostr "))
    assert r.status_code == 200
    assert created.await_args.args[1:3] == ("hashtag", "nostr")


# --------------------------------------------------------------------------
# relays
# --------------------------------------------------------------------------


def test_relays_may_be_omitted(api):
    client, created = api
    body = _body()
    del body["relays"]
    assert client.post("/shorturl", json=body).status_code == 200
    assert created.await_args.args[3] == []


def test_too_many_relays_is_a_422(api):
    client, created = api
    relays = [f"wss://r{i}.example" for i in range(8)]
    assert client.post("/shorturl", json=_body(relays=relays)).status_code == 422
    assert created.await_count == 0, "rejected before the service is reached"


def test_seven_relays_is_still_accepted(api):
    client, _ = api
    relays = [f"wss://r{i}.example" for i in range(7)]
    assert client.post("/shorturl", json=_body(relays=relays)).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        "http://relay.example",
        "relay.example",
        "wss://",
        "",
        "   ",
        "wss://a b",
        "ws://x:99999",
    ],
)
def test_a_malformed_relay_is_a_422(api, bad):
    client, created = api
    assert client.post("/shorturl", json=_body(relays=[bad])).status_code == 422
    assert created.await_count == 0


def test_surrounding_whitespace_is_stripped_before_storage(api):
    """Validated *and* normalised — otherwise GET echoes back the padding."""
    client, created = api
    r = client.post("/shorturl", json=_body(relays=["  wss://r.example  "]))
    assert r.status_code == 200
    assert created.await_args.args[3] == ["wss://r.example"]


@pytest.mark.parametrize("kind,key", [("profile", PK_HEX), ("hashtag", "nostr")])
@pytest.mark.parametrize(
    "search",
    [
        "wss://search.brainstorm.world",
        "WSS://Search.Brainstorm.World/",
        " wss://search.brainstorm.world/ ",
    ],
)
def test_the_search_relay_is_never_stored_as_a_hint(api, kind, key, search):
    client, created = api
    r = client.post(
        "/shorturl",
        json=_body(kind=kind, key=key, relays=[search, "wss://r.example"]),
    )
    assert r.status_code == 200
    assert created.await_args.args[3] == ["wss://r.example"]


# --------------------------------------------------------------------------
# resolve — the path only admits a plausible code
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc12", "A" * 33, "AB3XK9Q-", "AB3XK9QZ.png"])
def test_a_malformed_code_is_a_422_before_any_lookup(api, monkeypatch, bad):
    client, _ = api
    lookup = AsyncMock()
    monkeypatch.setattr("app.routers.shorturl.router.get_short_url_entity", lookup)
    assert client.get(f"/shorturl/{bad}").status_code == 422
    lookup.assert_not_awaited()


@pytest.mark.parametrize("code", ["ab3xk9", "AB3XK9QZ", "A" * 32])
def test_a_plausible_code_reaches_the_lookup(api, monkeypatch, code):
    client, _ = api
    lookup = AsyncMock(
        return_value=ShortUrlEntity(kind="profile", key=PK_HEX, relays=[], path=PATH)
    )
    monkeypatch.setattr("app.routers.shorturl.router.get_short_url_entity", lookup)
    assert client.get(f"/shorturl/{code}").status_code == 200
    lookup.assert_awaited_once()
