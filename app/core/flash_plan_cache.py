"""Flash's plans, cached, because a public page now depends on Flash.

`/billing/plans` is unauthenticated. Without this, every anonymous visit would
be a Flash call — their quota and their latency both become ours — and a Flash
outage would empty the pricing page.

So two entries per service, and the second is the point:

- a short-lived copy, which is what stops repeat visits reaching Flash at all;
- a last-known-good copy with no expiry, served whenever Flash cannot be read.
  A TTL on that one would make an outage longer than the TTL do exactly what it
  exists to prevent.

Only "we could not ask" falls back on the stored copy. Flash answering that a
service does not exist is an answer, and one only an operator can act on — so
it is logged as a fault rather than cached, but it still costs that service its
plans alone, never the page.
"""

import asyncio
import json

from app.core.config import settings
from app.core.flash import (
    FlashPlan,
    FlashServiceMissing,
    FlashUnavailable,
    fetch_service_plans_raw,
    parse_plan,
)
from app.core.loggr import loggr
from app.core.redis_db import redis_client

logger = loggr.get_logger(__name__)

_PREFIX = "flash:plans:"

# One in-flight read per service. Without it a burst arriving on an expired key
# is one Flash call each, which is the traffic the cache exists to absorb.
_reads: dict[str, asyncio.Lock] = {}


def fresh_key(service_id: str) -> str:
    return f"{_PREFIX}{service_id}"


def last_good_key(service_id: str) -> str:
    return f"{_PREFIX}lkg:{service_id}"


async def read_service_plans(service_id: str) -> list[FlashPlan]:
    """Every plan Flash offers on one service, from cache where possible.

    Raises `FlashUnavailable` only when Flash is unreadable AND we have never
    read it — the caller must be able to tell that from an empty catalogue.
    `FlashServiceMissing` passes through: our configuration names nothing.
    `read_plans_for_services` catches both per service, so one bad id degrades
    the page rather than refusing it.
    """
    if settings.flash_mock_enabled:
        # Deliberately ahead of the cache and never written to it: the LOCAL
        # dev endpoint that sets a plan would otherwise appear not to work
        # until the TTL ran out.
        from app.core import flash_mock

        return [parse_plan(plan) for plan in flash_mock.plans_for(service_id)]

    cached = await _load(fresh_key(service_id))
    if cached is not None:
        return cached

    async with _reads.setdefault(service_id, asyncio.Lock()):
        # Whoever held the lock has just refreshed it.
        cached = await _load(fresh_key(service_id))
        if cached is not None:
            return cached
        return await _refresh(service_id)


async def read_plans_for_services(
    service_ids: set[str],
) -> dict[tuple[str, str], FlashPlan]:
    """Every plan across several services, keyed the way a mapping names one.

    One read per service rather than one per mapping: the cache would absorb
    the rest, but a cold page would still make a call per row.

    Neither failure is allowed to cost the other services their plans. An
    unreadable Flash has already tried its last known copy and has nothing; a
    service Flash does not hold is a fault in our own configuration. Both end
    the same way here — that service contributes nothing and the rest still
    render — because one mistyped id must not blank a public page.

    The two are still logged apart: only one of them is anyone's to fix.
    """
    plans: dict[tuple[str, str], FlashPlan] = {}
    for service_id in sorted(service_ids):
        try:
            found = await read_service_plans(service_id)
        except FlashUnavailable:
            logger.warning(
                "Flash is unreadable and nothing is cached for service %s; its "
                "plans are omitted",
                service_id,
            )
            continue
        except FlashServiceMissing:
            logger.error(
                "Flash holds no service %s; the plans mapped to it cannot be "
                "sold until that id is corrected",
                service_id,
            )
            continue
        for plan in found:
            plans[(service_id, plan.id)] = plan
    return plans


async def _refresh(service_id: str) -> list[FlashPlan]:
    try:
        body = await fetch_service_plans_raw(service_id)
    except FlashUnavailable:
        last_good = await _load(last_good_key(service_id))
        if last_good is None:
            raise
        logger.warning(
            "Flash is unreadable; serving the last known plans for service %s",
            service_id,
        )
        return last_good

    raw = body.get("plans")
    plans = [plan for plan in raw if isinstance(plan, dict)] if isinstance(raw, list) else []
    await _store(service_id, plans)
    return [parse_plan(plan) for plan in plans]


async def _load(key: str) -> list[FlashPlan] | None:
    """A cache that cannot be read is a cache miss, never a failure: Redis being
    down must not take the pricing page with it."""
    try:
        stored = await redis_client.get(key)
    except Exception:
        logger.warning("Could not read %s from Redis", key, exc_info=True)
        return None
    if not stored:
        return None
    try:
        rows = json.loads(stored)
    except ValueError:
        return None
    return [parse_plan(row) for row in rows if isinstance(row, dict)]


async def _store(service_id: str, plans: list[dict]) -> None:
    payload = json.dumps(plans)
    try:
        await redis_client.set(
            fresh_key(service_id), payload, ex=settings.flash_plans_cache_ttl_seconds
        )
        await redis_client.set(last_good_key(service_id), payload)
    except Exception:
        logger.warning("Could not cache Flash plans for %s", service_id, exc_info=True)
