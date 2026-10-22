"""Login (challenge + verify) is rate-limited per client IP.

Driven through the real routes; Redis is a clock-controlled fake so the
fixed window can be stepped past, and the Observer table is mocked.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from nostr_sdk import EventBuilder, Keys, Kind, Tag

from app.api import app as fastapi_app
from app.core.config import settings
from app.core.database import get_db
from app.utils.rate_limiting import rate_limiting

_IP = "203.0.113.7"
_OTHER_IP = "198.51.100.20"


class _ClockRedis:
    """The redis surface login + the limiter use, with TTLs on a fake clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self._data: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}

    def _live(self, key: str) -> bool:
        deadline = self._expires_at.get(key)
        if deadline is not None and deadline <= self.now:
            self._data.pop(key, None)
            self._expires_at.pop(key, None)
        return key in self._data

    async def get(self, key):
        return self._data[key] if self._live(key) else None

    async def set(self, key, value, ex=None, nx=False):
        if nx and self._live(key):
            return None
        self._data[key] = str(value)
        if ex is not None:
            self._expires_at[key] = self.now + ex
        else:
            self._expires_at.pop(key, None)
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self._live(key):
                del self._data[key]
                self._expires_at.pop(key, None)
                removed += 1
        return removed

    async def getdel(self, key):
        value = await self.get(key)
        await self.delete(key)
        return value

    async def incr(self, key):
        value = int(self._data[key]) + 1 if self._live(key) else 1
        self._data[key] = str(value)
        return value

    async def expire(self, key, seconds):
        if not self._live(key):
            return False
        self._expires_at[key] = self.now + seconds
        return True

    async def ttl(self, key):
        if not self._live(key):
            return -2
        deadline = self._expires_at.get(key)
        return -1 if deadline is None else int(deadline - self.now + 0.999)


@pytest.fixture
def redis(monkeypatch) -> _ClockRedis:
    fake = _ClockRedis()
    monkeypatch.setattr(rate_limiting, "get_redis_client", lambda: fake)
    monkeypatch.setattr("app.services.auth_service.redis_client", fake)
    return fake


@pytest.fixture
def observers(monkeypatch) -> set[str]:
    """Pubkeys that already have an Observer row; verify adds to it."""
    existing: set[str] = set()

    async def _exists(_db, pubkey):
        return pubkey in existing

    async def _get_or_create(_db, pubkey):
        created = pubkey not in existing
        existing.add(pubkey)
        return object(), created

    monkeypatch.setattr(
        "app.services.auth_service.brainstorm_nsec_exists_by_pubkey_on_db",
        _exists,
    )
    monkeypatch.setattr(
        "app.services.auth_service.get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        _get_or_create,
    )
    monkeypatch.setattr(
        "app.services.auth_service.current_session_version",
        AsyncMock(return_value=0),
    )

    async def _no_db():
        yield AsyncMock()

    fastapi_app.dependency_overrides[get_db] = _no_db
    yield existing
    fastapi_app.dependency_overrides.pop(get_db, None)


def _challenge(client, pubkey: str, ip: str = _IP):
    return client.get(f"/authChallenge/{pubkey}", headers={"X-Forwarded-For": ip})


def _login(client, keys: Keys, ip: str = _IP):
    pubkey = keys.public_key().to_hex()
    response = _challenge(client, pubkey, ip)
    if response.status_code != 200:
        return response
    challenge = response.json()["data"]["challenge"]
    event = (
        EventBuilder(Kind(22242), "")
        .tags(
            [Tag.parse(["t", "brainstorm_login"]), Tag.parse(["challenge", challenge])]
        )
        .sign_with_keys(keys)
    )
    return client.post(
        f"/authChallenge/{pubkey}/verify",
        json={"signed_event": json.loads(event.as_json())},
        headers={"X-Forwarded-For": ip},
    )


# --------------------------------------------------------------------------
# Challenge: per-IP, never per-pubkey
# --------------------------------------------------------------------------


def test_challenges_under_the_limit_are_served(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "login_challenge_rate_limit_per_minute", 3)
    pubkey = Keys.generate().public_key().to_hex()

    statuses = [_challenge(client, pubkey).status_code for _ in range(3)]

    assert statuses == [200, 200, 200]


def test_a_challenge_over_the_limit_is_429_with_retry_after(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "login_challenge_rate_limit_per_minute", 3)
    pubkey = Keys.generate().public_key().to_hex()
    for _ in range(3):
        _challenge(client, pubkey)

    response = _challenge(client, pubkey)

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60


def test_the_challenge_bucket_resets_after_the_window(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "login_challenge_rate_limit_per_minute", 3)
    pubkey = Keys.generate().public_key().to_hex()
    for _ in range(4):
        _challenge(client, pubkey)

    redis.now += 60

    assert _challenge(client, pubkey).status_code == 200


def test_challenges_are_bucketed_by_ip_not_pubkey(client, redis, monkeypatch):
    """A victim's pubkey can be hammered from elsewhere without locking the victim out."""
    monkeypatch.setattr(settings, "login_challenge_rate_limit_per_minute", 3)
    victim = Keys.generate().public_key().to_hex()
    for _ in range(10):
        _challenge(client, victim, ip=_OTHER_IP)

    assert _challenge(client, victim).status_code == 200


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


def test_verifies_over_the_limit_are_429(client, redis, observers, monkeypatch):
    monkeypatch.setattr(settings, "login_verify_rate_limit_per_minute", 2)
    keys = Keys.generate()
    observers.add(keys.public_key().to_hex())

    statuses = [_login(client, keys).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


def test_the_verify_bucket_resets_after_the_window(
    client, redis, observers, monkeypatch
):
    monkeypatch.setattr(settings, "login_verify_rate_limit_per_minute", 1)
    keys = Keys.generate()
    observers.add(keys.public_key().to_hex())
    _login(client, keys)
    assert _login(client, keys).status_code == 429

    redis.now += 60

    assert _login(client, keys).status_code == 200


def test_new_observers_per_ip_are_capped(client, redis, observers, monkeypatch):
    monkeypatch.setattr(settings, "login_new_observer_rate_limit_per_hour", 2)

    statuses = [_login(client, Keys.generate()).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]
    assert len(observers) == 2
    assert 1 <= int(_login(client, Keys.generate()).headers["Retry-After"]) <= 3600


def test_existing_observers_still_log_in_past_the_new_observer_cap(
    client, redis, observers, monkeypatch
):
    monkeypatch.setattr(settings, "login_new_observer_rate_limit_per_hour", 1)
    returning = Keys.generate()
    observers.add(returning.public_key().to_hex())
    _login(client, Keys.generate())
    assert _login(client, Keys.generate()).status_code == 429

    assert _login(client, returning).status_code == 200


def test_a_failed_verify_does_not_spend_the_new_observer_bucket(
    client, redis, observers, monkeypatch
):
    monkeypatch.setattr(settings, "login_new_observer_rate_limit_per_hour", 1)
    stranger = Keys.generate().public_key().to_hex()
    for _ in range(3):
        client.post(
            f"/authChallenge/{stranger}/verify",
            json={"signed_event": {"tags": []}},
            headers={"X-Forwarded-For": _IP},
        )

    assert _login(client, Keys.generate()).status_code == 200


def test_the_new_observer_bucket_resets_after_the_window(
    client, redis, observers, monkeypatch
):
    monkeypatch.setattr(settings, "login_new_observer_rate_limit_per_hour", 1)
    _login(client, Keys.generate())
    assert _login(client, Keys.generate()).status_code == 429

    redis.now += 3600

    assert _login(client, Keys.generate()).status_code == 200


# --------------------------------------------------------------------------
# Bucketing + observability
# --------------------------------------------------------------------------


def test_ipv6_clients_share_a_bucket_across_their_64(client, redis, monkeypatch):
    """One host holds a whole /64; rotating within it must not win requests."""
    monkeypatch.setattr(settings, "login_challenge_rate_limit_per_minute", 3)
    pubkey = Keys.generate().public_key().to_hex()
    for ip in (
        "2001:db8:aa:bb::1",
        "2001:db8:aa:bb::2",
        "2001:db8:aa:bb:abcd:ef01:2345:6789",
    ):
        _challenge(client, pubkey, ip=ip)

    assert _challenge(client, pubkey, ip="2001:db8:aa:bb::ffff").status_code == 429


def test_neighbouring_ipv6_64s_have_separate_buckets(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "login_challenge_rate_limit_per_minute", 1)
    pubkey = Keys.generate().public_key().to_hex()
    _challenge(client, pubkey, ip="2001:db8:aa:bb::1")

    assert _challenge(client, pubkey, ip="2001:db8:aa:bc::1").status_code == 200


def test_each_429_is_logged_with_its_bucket(client, redis, observers, monkeypatch):
    """The repo's loggr doesn't propagate, so assert on the logger itself."""
    warnings = MagicMock()
    monkeypatch.setattr(rate_limiting.logger, "warning", warnings)
    monkeypatch.setattr(settings, "login_new_observer_rate_limit_per_hour", 1)
    _login(client, Keys.generate())
    warnings.assert_not_called()

    _login(client, Keys.generate())

    warnings.assert_called_once()
    logged = warnings.call_args.args[0] % warnings.call_args.args[1:]
    assert f"rate_limit:login_new_observer:{_IP}" in logged
