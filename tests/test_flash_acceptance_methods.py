"""The account's acceptance methods — the only thing Flash publishes that says
how a subscription is paid for.

`GET /settings` maps opaque `amt_…` tokens onto a kind and a provider; a plan
names the tokens it accepts. Between them they answer "Lightning or card" for a
plan that accepts exactly one, and nothing at all for a plan that accepts both.

Cached for the same reason the plans are: this is read on every signed-in
billing page and on every load of the admin roster, and none of them may put a
Flash call in front of the render.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core import flash_settings_cache as cache
from app.core.flash import FlashUnavailable

SETTINGS = {
    "livemode": True,
    "acceptanceMethods": [
        {
            "token": "amt_ln",
            "kind": "lightning.invoice",
            "provider": "lightning",
            "label": "Ln Wallet",
        },
        {
            "token": "amt_card",
            "kind": "card.vaulted",
            "provider": "card",
            "label": "Card",
        },
    ],
}


class FakeRedis:
    def __init__(self, seeded: dict | None = None):
        self.store = dict(seeded or {})
        self.sets: list[tuple[str, str, int | None]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.sets.append((key, value, ex))


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cache, "redis_client", fake)
    return fake


def _flash(monkeypatch, body=None, side_effect=None):
    reader = AsyncMock(
        return_value=SETTINGS if body is None else body, side_effect=side_effect
    )
    monkeypatch.setattr(cache, "fetch_settings_raw", reader)
    return reader


def test_each_token_says_how_a_subscription_on_it_is_paid_for(redis, monkeypatch):
    _flash(monkeypatch)

    assert asyncio.run(cache.read_acceptance_methods()) == {
        "amt_ln": "lightning",
        "amt_card": "card",
    }


def test_a_second_read_does_not_ask_flash_again(redis, monkeypatch):
    reader = _flash(monkeypatch)

    asyncio.run(cache.read_acceptance_methods())
    asyncio.run(cache.read_acceptance_methods())

    assert reader.await_count == 1


def test_an_unreachable_flash_serves_the_last_copy_we_had(redis, monkeypatch):
    redis.store[cache.LAST_GOOD_KEY] = json.dumps({"amt_ln": "lightning"})
    _flash(monkeypatch, side_effect=FlashUnavailable("down"))

    assert asyncio.run(cache.read_acceptance_methods()) == {"amt_ln": "lightning"}


def test_an_unreachable_flash_with_nothing_cached_resolves_nothing(
    redis, monkeypatch
):
    """Not an error the caller has to handle: a method we cannot resolve is a
    method we do not show, which is the same answer as an ambiguous plan."""
    _flash(monkeypatch, side_effect=FlashUnavailable("down"))

    assert asyncio.run(cache.read_acceptance_methods()) == {}


def test_a_method_naming_no_provider_falls_back_to_its_kind(redis, monkeypatch):
    """`kind` is `{provider}.{instrument}`, so its first segment is the same
    answer — read only when Flash sends no provider of its own."""
    _flash(
        monkeypatch,
        body={"acceptanceMethods": [{"token": "amt_ln", "kind": "lightning.invoice"}]},
    )

    assert asyncio.run(cache.read_acceptance_methods()) == {"amt_ln": "lightning"}


def test_the_local_fake_answers_without_reaching_flash_or_the_cache(
    redis, monkeypatch
):
    """There is no Flash sandbox, so the fake is how the paid paths are
    rehearsed — and a rehearsal that reached the real `/settings` would be a
    live call from a machine holding no answer worth having."""
    from app.core import flash_mock
    from app.core.config import settings

    reader = _flash(monkeypatch)
    monkeypatch.setattr(settings, "flash_mock_enabled", True)
    flash_mock.clear()
    flash_mock.set_acceptance_methods({"amt_local": "lightning"})

    assert asyncio.run(cache.read_acceptance_methods()) == {"amt_local": "lightning"}
    reader.assert_not_awaited()
    assert redis.sets == []
    flash_mock.clear()


def test_a_method_we_cannot_read_is_dropped_rather_than_named(redis, monkeypatch):
    """A token with nothing to call it is a token that resolves to nothing —
    never to a placeholder that would render on somebody's billing page."""
    _flash(
        monkeypatch,
        body={
            "acceptanceMethods": [
                {"token": "amt_mystery"},
                {"kind": "card.vaulted", "provider": "card"},
                "not an object",
            ]
        },
    )

    assert asyncio.run(cache.read_acceptance_methods()) == {}
