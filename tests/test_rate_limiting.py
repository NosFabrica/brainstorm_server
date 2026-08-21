"""Per-IP rate limiting identifies the caller from the trusted proxy hop.

The ingress *appends* the peer address to ``X-Forwarded-For`` rather than
replacing the header, so a client-supplied value survives at the front. Reading
the first entry therefore reads attacker-controlled text; the trustworthy entry
is the one our own proxy wrote, counting back ``settings.trusted_proxy_hops``
from the right.

Issue: .scratch/shorturl/issues/01-rate-limit-utility.md
"""

import asyncio

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock
from starlette.requests import Request

from app.core.config import settings
from app.utils.rate_limiting import rate_limiting
from app.utils.rate_limiting.rate_limiting import (
    GRAPERANK_POLICY,
    RateLimitPolicy,
    resolve_client_ip,
    validate_rate_limit,
)

_PEER = "198.51.100.4"
_REAL = "203.0.113.7"
_SPOOF = "9.9.9.9"


def _request(xff: str | None = None, peer: str | None = _PEER) -> Request:
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/shorturl",
        "headers": [(b"x-forwarded-for", xff.encode())] if xff is not None else [],
    }
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


class _FakeRedis:
    """Enough of the redis surface for the counter plus the shortener."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, nx: bool = False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limiting, "get_redis_client", lambda: fake)
    return fake


# --------------------------------------------------------------------------
# resolve_client_ip
# --------------------------------------------------------------------------


def test_ignores_a_spoofed_leading_hop():
    """The regression: a client-supplied entry must not be mistaken for the caller."""
    assert resolve_client_ip(_request(f"{_SPOOF}, {_REAL}")) == _REAL


def test_uses_the_sole_hop_when_the_client_sent_nothing():
    """With no client-supplied value the ingress leaves exactly its own entry."""
    assert resolve_client_ip(_request(_REAL)) == _REAL


def test_ignores_several_spoofed_hops():
    assert (
        resolve_client_ip(_request(f"{_SPOOF}, 10.0.0.1, 172.16.0.9, {_REAL}")) == _REAL
    )


def test_falls_back_to_the_direct_peer_without_the_header():
    assert resolve_client_ip(_request()) == _PEER


def test_falls_back_to_the_direct_peer_when_the_header_is_blank():
    assert resolve_client_ip(_request("   ,  ")) == _PEER


def test_tolerates_whitespace_around_hops():
    assert resolve_client_ip(_request(f"  {_SPOOF} ,   {_REAL}   ")) == _REAL


def test_resolves_to_unknown_without_a_header_or_a_peer():
    assert resolve_client_ip(_request(peer=None)) == "unknown"


def test_honours_the_trusted_hop_count(monkeypatch):
    """Putting a CDN in front shifts which entry is ours — one setting."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert resolve_client_ip(_request(f"{_SPOOF}, {_REAL}, 192.0.2.50")) == _REAL


def test_falls_back_to_the_peer_when_the_chain_is_shorter_than_expected(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert resolve_client_ip(_request(_REAL)) == _PEER


# --------------------------------------------------------------------------
# validate_rate_limit
# --------------------------------------------------------------------------


def _apply(ip: str, policy: RateLimitPolicy) -> None:
    """Drive the async limiter from a sync test — the suite has no asyncio plugin."""
    asyncio.run(validate_rate_limit(ip, policy))


_PROBE = RateLimitPolicy(key_prefix="probe", limit=10, window_seconds=60)


def _apply_graperank(ip: str) -> None:
    _apply(ip, GRAPERANK_POLICY)


def test_every_policy_gets_its_own_bucket(fake_redis):
    _apply(_REAL, RateLimitPolicy("shorturl_create", limit=5, window_seconds=1))
    assert f"rate_limit:shorturl_create:{_REAL}" in fake_redis.store


def test_graperank_counter_is_scoped_by_name(fake_redis):
    """Previously unscoped. See issue 01 — the key move is deliberate."""
    _apply_graperank(_REAL)
    assert f"rate_limit:graperank:{_REAL}" in fake_redis.store
    assert f"rate_limit:{_REAL}" not in fake_redis.store


def test_graperank_limit_and_window_are_unchanged(fake_redis):
    assert GRAPERANK_POLICY.limit == 3
    assert GRAPERANK_POLICY.window_seconds == 1800

    key = f"rate_limit:graperank:{_REAL}"
    for _ in range(GRAPERANK_POLICY.limit):
        _apply_graperank(_REAL)

    assert fake_redis.expiries[key] == GRAPERANK_POLICY.window_seconds

    with pytest.raises(HTTPException) as excinfo:
        _apply_graperank(_REAL)
    assert excinfo.value.status_code == 429


def test_the_window_expiry_is_set_once_on_the_first_hit(fake_redis):
    key = f"rate_limit:probe:{_REAL}"
    _apply(_REAL, _PROBE)
    fake_redis.expiries[key] = "untouched"
    _apply(_REAL, _PROBE)
    assert fake_redis.expiries[key] == "untouched"


def test_separate_callers_do_not_share_a_bucket(fake_redis):
    other = "192.0.2.99"
    _apply(_REAL, _PROBE)
    _apply(other, _PROBE)

    assert fake_redis.store[f"rate_limit:probe:{_REAL}"] == "1"
    assert fake_redis.store[f"rate_limit:probe:{other}"] == "1"


# --------------------------------------------------------------------------
# End to end through the shortener, which is the endpoint that has no auth
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spoofed", [True, False])
def test_a_burst_is_throttled_whether_or_not_the_caller_spoofs(
    client, monkeypatch, fake_redis, spoofed
):
    """Rotating a forged leading hop must not win extra requests.

    The shortener's storage is mocked out: this exercises the limiter, which
    runs as a route dependency before the handler is ever entered.
    """
    from app.core.database import get_db
    from app.api import app as fastapi_app
    from app.schemas.schemas import ShortUrlContent

    async def _no_db():
        yield None

    fastapi_app.dependency_overrides[get_db] = _no_db
    monkeypatch.setattr(
        "app.routers.shorturl.router.create_short_url",
        AsyncMock(
            return_value=("AB3XK9QZ", ShortUrlContent(pubkey="a" * 64, relays=[]))
        ),
    )
    try:
        body = {"pubkey": "a" * 64, "relays": []}

        statuses = []
        for attempt in range(4):
            chain = f"10.0.0.{attempt}, {_REAL}" if spoofed else _REAL
            response = client.post(
                "/shorturl", json=body, headers={"X-Forwarded-For": chain}
            )
            statuses.append(response.status_code)

        assert statuses[0] == 200, f"first request should be allowed, got {statuses[0]}"
        assert statuses[1:] == [429, 429, 429], statuses
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
