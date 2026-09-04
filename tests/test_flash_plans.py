"""The cached read of Flash's plans — what a public, unauthenticated page rests on.

Two things this has to be true of, and they are the reason the cache exists at
all rather than being an optimisation: anonymous traffic must not drive our
Flash quota, and a Flash outage must not empty the pricing page.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core import flash_plan_cache as cache
from app.core.flash import FlashServiceMissing, FlashUnavailable

SERVICE = "9c1e"

FLASH_PLAN = {
    "id": "4f2a",
    "serviceId": SERVICE,
    "name": "Monthly",
    "description": "The usual one",
    "features": ["weekly recalculation"],
    "notIncluded": ["priority support"],
    "amount": "100",
    "currency": "USD",
    "billingInterval": "monthly",
    "status": "active",
    "sortOrder": 3,
    "signupUrl": "https://flash.example/subscriptions/signup/9c1e/4f2a",
}


class FakeRedis:
    """Enough of redis for a get/set cache. Expiry is redis's job, not ours —
    a key the test wants expired is simply absent."""

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


def _flash(monkeypatch, plans=(FLASH_PLAN,), side_effect=None):
    reader = AsyncMock(
        return_value={"livemode": True, "service": {"id": SERVICE}, "plans": list(plans)},
        side_effect=side_effect,
    )
    monkeypatch.setattr(cache, "fetch_service_plans_raw", reader)
    return reader


def test_a_second_visit_does_not_ask_flash_again(redis, monkeypatch):
    """`/billing/plans` is unauthenticated, so a page anyone can refresh must
    not turn into one Flash call per visitor."""
    reader = _flash(monkeypatch)

    first = asyncio.run(cache.read_service_plans(SERVICE))
    second = asyncio.run(cache.read_service_plans(SERVICE))

    reader.assert_awaited_once()
    assert [plan.id for plan in first] == [plan.id for plan in second] == ["4f2a"]


def test_an_operator_read_asks_flash_now_and_refreshes_the_public_copy(
    redis, monkeypatch
):
    """The admin editing a plan in Flash wants what it is now, not what the
    cache held five minutes ago — and their look should catch the page up."""
    reader = _flash(monkeypatch)
    asyncio.run(cache.read_service_plans(SERVICE))

    asyncio.run(cache.read_service_plans(SERVICE, fresh=True))

    assert reader.await_count == 2
    assert [key for key, _, _ in redis.sets].count(cache.fresh_key(SERVICE)) == 2


def test_an_unreachable_flash_serves_the_last_copy_we_had(redis, monkeypatch):
    """A plan must not vanish from the pricing page because Flash blinked."""
    _flash(monkeypatch)
    asyncio.run(cache.read_service_plans(SERVICE))
    # The short-lived entry has expired; only the last-known-good copy remains.
    del redis.store[cache.fresh_key(SERVICE)]
    _flash(monkeypatch, side_effect=FlashUnavailable("down"))

    plans = asyncio.run(cache.read_service_plans(SERVICE))

    assert [plan.name for plan in plans] == ["Monthly"]


def test_an_unreachable_flash_with_nothing_cached_says_so(redis, monkeypatch):
    """Nothing to serve is not an empty catalogue — the caller has to be able
    to tell "we could not ask" from "there are no plans"."""
    _flash(monkeypatch, side_effect=FlashUnavailable("down"))

    with pytest.raises(FlashUnavailable):
        asyncio.run(cache.read_service_plans(SERVICE))


def test_a_service_flash_does_not_have_is_reported_as_a_misconfiguration(
    redis, monkeypatch
):
    """A 404 here is not an empty page: the service id in our plan mappings
    names something Flash does not hold, which someone has to fix."""
    _flash(monkeypatch, side_effect=FlashServiceMissing(SERVICE))

    with pytest.raises(FlashServiceMissing):
        asyncio.run(cache.read_service_plans(SERVICE))


def test_a_plan_carries_everything_the_pricing_page_stopped_storing(
    redis, monkeypatch
):
    """Name, price, currency, cadence, ordering, copy and where to buy it —
    all Flash's now."""
    _flash(monkeypatch)

    plan = asyncio.run(cache.read_service_plans(SERVICE))[0]

    assert plan.signup_url == "https://flash.example/subscriptions/signup/9c1e/4f2a"
    assert plan.id == "4f2a"
    assert plan.name == "Monthly"
    assert plan.description == "The usual one"
    assert plan.amount_minor == 100
    assert plan.status == "active"
    assert plan.currency == "USD"
    assert plan.billing_interval == "monthly"
    assert plan.sort_order == 3
    assert plan.features == ["weekly recalculation"]
    assert plan.not_included == ["priority support"]


def test_the_local_fake_answers_without_reaching_flash_or_the_cache(
    redis, monkeypatch
):
    """There is no Flash sandbox, so the fake is how the paid paths are
    rehearsed locally — including the pricing page they start on. Not cached:
    the dev endpoint that sets a plan would appear not to work until the TTL
    ran out.
    """
    from app.core import flash_mock
    from app.core.config import settings

    reader = _flash(monkeypatch)
    monkeypatch.setattr(settings, "flash_mock_enabled", True)
    flash_mock.clear()
    flash_mock.set_plan({"id": "4f2a", "serviceId": SERVICE, "name": "Local", "amount": "1"})

    plans = asyncio.run(cache.read_service_plans(SERVICE))

    assert [plan.name for plan in plans] == ["Local"]
    reader.assert_not_awaited()
    assert redis.sets == []
    flash_mock.clear()


def test_one_unreadable_service_does_not_lose_the_others(redis, monkeypatch):
    """Across services the failures are told apart: one Flash cannot be read for
    contributes nothing, and the rest of the catalogue still comes back."""
    reads = {
        SERVICE: [FLASH_PLAN],
        "dead": FlashUnavailable("down"),
    }

    async def _read(service_id):
        answer = reads[service_id]
        if isinstance(answer, Exception):
            raise answer
        return [cache.parse_plan(plan) for plan in answer]

    monkeypatch.setattr(cache, "read_service_plans", _read)

    plans = asyncio.run(cache.read_plans_for_services({SERVICE, "dead"}))

    assert list(plans) == [(SERVICE, "4f2a")]


def test_a_missing_service_carries_the_id_that_has_to_be_corrected(
    redis, monkeypatch
):
    """The id is the whole content of the fix, so it rides on the exception
    rather than only in a log line."""
    _flash(monkeypatch, side_effect=FlashServiceMissing(SERVICE))

    with pytest.raises(FlashServiceMissing) as raised:
        asyncio.run(cache.read_service_plans(SERVICE))

    assert raised.value.service_id == SERVICE


def test_one_mistyped_service_does_not_cost_the_others_their_plans(
    redis, monkeypatch
):
    """A public page reads this. One id an operator got wrong must cost that
    service its plans and nothing else."""

    async def per_service(service_id):
        if service_id == "typo":
            raise FlashServiceMissing(service_id)
        return [cache.parse_plan(FLASH_PLAN)]

    monkeypatch.setattr(cache, "read_service_plans", per_service)

    found = asyncio.run(cache.read_plans_for_services({"typo", SERVICE}))

    assert [key[0] for key in found] == [SERVICE]


def test_an_amount_we_cannot_read_is_unknown_rather_than_zero(redis, monkeypatch):
    """Flash sends amounts as strings in minor units. Zero would render as
    "Free" on a public pricing page for a plan somebody is charged for."""
    _flash(monkeypatch, plans=[{**FLASH_PLAN, "amount": "1.00"}])

    assert asyncio.run(cache.read_service_plans(SERVICE))[0].amount_minor is None


def test_the_last_good_copy_is_kept_without_an_expiry(redis, monkeypatch):
    """A TTL on the fallback would make an outage lasting longer than the TTL
    empty the page — which is the case it exists for."""
    _flash(monkeypatch)

    asyncio.run(cache.read_service_plans(SERVICE))

    written = dict((key, ex) for key, _, ex in redis.sets)
    assert written[cache.fresh_key(SERVICE)] is not None
    assert written[cache.last_good_key(SERVICE)] is None
    assert json.loads(redis.store[cache.last_good_key(SERVICE)])[0]["id"] == "4f2a"
