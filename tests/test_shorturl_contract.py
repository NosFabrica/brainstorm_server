"""The shortener's HTTP contract: validation shape and wire format.

Validation belongs in the request schema, so bad input is a 422 from the
framework rather than a hand-rolled 400 in service code. The wire format is
frozen: the response field is `shortCode`, whatever the Python attribute is
called.
"""

import pytest
from unittest.mock import AsyncMock

from app.api import app as fastapi_app
from app.core.database import get_db
from app.routers.shorturl.router import rate_limit_create_short_url
from app.schemas.schemas import CreatedShortUrl, ShortUrlContent

PK_HEX = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"
PK_NPUB = "npub17ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqr5qjta"


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
            short_code="AB3XK9QZ", content=ShortUrlContent(pubkey=PK_HEX, relays=[])
        )
    )
    monkeypatch.setattr("app.routers.shorturl.router.create_short_url", created)
    yield client, created
    # no teardown needed: conftest's `client` fixture clears dependency_overrides


# --------------------------------------------------------------------------
# wire format — must not move, the frontend is built against it
# --------------------------------------------------------------------------


def test_the_response_field_is_still_short_code_camelcased(api):
    client, _ = api
    body = client.post("/shorturl", json={"pubkey": PK_HEX, "relays": []}).json()
    assert body["data"]["shortCode"] == "AB3XK9QZ"
    assert "short_code" not in body["data"]


def test_the_python_attribute_is_snake_case():
    """The alias bridges; the attribute itself follows Python convention."""
    model = CreatedShortUrl(
        short_code="AB3XK9QZ", content=ShortUrlContent(pubkey=PK_HEX, relays=[])
    )
    assert model.short_code == "AB3XK9QZ"
    assert model.model_dump(by_alias=True)["shortCode"] == "AB3XK9QZ"


# --------------------------------------------------------------------------
# validation lives in the schema, so the framework answers 422
# --------------------------------------------------------------------------


def test_too_many_relays_is_a_422(api):
    client, created = api
    relays = [f"wss://r{i}.example" for i in range(8)]
    assert (
        client.post("/shorturl", json={"pubkey": PK_HEX, "relays": relays}).status_code
        == 422
    )
    assert created.await_count == 0, "rejected before the service is reached"


def test_seven_relays_is_still_accepted(api):
    client, _ = api
    relays = [f"wss://r{i}.example" for i in range(7)]
    assert (
        client.post("/shorturl", json={"pubkey": PK_HEX, "relays": relays}).status_code
        == 200
    )


@pytest.mark.parametrize(
    "bad", ["http://relay.example", "relay.example", "wss://", "", "   "]
)
def test_a_malformed_relay_is_a_422(api, bad):
    client, created = api
    assert (
        client.post("/shorturl", json={"pubkey": PK_HEX, "relays": [bad]}).status_code
        == 422
    )
    assert created.await_count == 0


def test_surrounding_whitespace_is_stripped_before_storage(api):
    """Validated *and* normalised — otherwise GET echoes back the padding."""
    client, created = api
    r = client.post(
        "/shorturl", json={"pubkey": PK_HEX, "relays": ["  wss://r.example  "]}
    )
    assert r.status_code == 200
    assert created.await_args.args[2] == ["wss://r.example"]


def test_omitting_relays_is_still_a_422(api):
    """`relays` was required before this ticket; it stays required."""
    client, created = api
    assert client.post("/shorturl", json={"pubkey": PK_HEX}).status_code == 422
    assert created.await_count == 0


def test_a_padded_pubkey_is_accepted_here_only(api):
    """The shortener strips at its own edge; the shared helper stays strict, so
    the other pubkey endpoints keep rejecting padded input."""
    from app.utils.nostr import resolve_pubkey_or_400

    client, created = api
    assert (
        client.post(
            "/shorturl", json={"pubkey": f"  {PK_HEX}  ", "relays": []}
        ).status_code
        == 200
    )
    assert created.await_args.args[1] == PK_HEX

    with pytest.raises(Exception):
        resolve_pubkey_or_400(f"  {PK_HEX}  ", "pubkey")


def test_an_empty_relay_list_is_still_valid(api):
    client, _ = api
    assert (
        client.post("/shorturl", json={"pubkey": PK_HEX, "relays": []}).status_code
        == 200
    )


# --------------------------------------------------------------------------
# pubkey: rejected if malformed, normalised to hex if npub
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "not-a-pubkey", "deadbeef", "z" * 64])
def test_a_malformed_pubkey_is_a_422(api, bad):
    client, created = api
    assert (
        client.post("/shorturl", json={"pubkey": bad, "relays": []}).status_code == 422
    )
    assert created.await_count == 0, "a bad pubkey must never reach storage"


def test_a_hex_pubkey_reaches_the_service_unchanged(api):
    client, created = api
    assert (
        client.post("/shorturl", json={"pubkey": PK_HEX, "relays": []}).status_code
        == 200
    )
    assert created.await_args.args[1] == PK_HEX


def test_an_npub_is_normalised_to_hex_before_storage(api):
    """Decided: npub is accepted and normalised, matching resolve_pubkey_or_400."""
    client, created = api
    assert (
        client.post("/shorturl", json={"pubkey": PK_NPUB, "relays": []}).status_code
        == 200
    )
    assert created.await_args.args[1] == PK_HEX, "stored form is always hex"


# --------------------------------------------------------------------------
# resolve — the path only admits a plausible code
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc12", "A" * 33, "AB3XK9Q-", "AB3XK9QZ.png"])
def test_a_malformed_code_is_a_422_before_any_lookup(api, monkeypatch, bad):
    client, _ = api
    lookup = AsyncMock()
    monkeypatch.setattr("app.routers.shorturl.router.get_short_url_content", lookup)
    assert client.get(f"/shorturl/{bad}").status_code == 422
    lookup.assert_not_awaited()


@pytest.mark.parametrize("code", ["ab3xk9", "AB3XK9QZ", "A" * 32])
def test_a_plausible_code_reaches_the_lookup(api, monkeypatch, code):
    client, _ = api
    lookup = AsyncMock(return_value=ShortUrlContent(pubkey=PK_HEX, relays=[]))
    monkeypatch.setattr("app.routers.shorturl.router.get_short_url_content", lookup)
    assert client.get(f"/shorturl/{code}").status_code == 200
    lookup.assert_awaited_once()
