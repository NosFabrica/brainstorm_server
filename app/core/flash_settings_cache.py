"""The account's acceptance methods, cached — what each `amt_…` token pays by.

Same shape and the same two reasons as `flash_plan_cache`: this is read on
every signed-in billing page and on every load of the admin roster, so a short
TTL is what keeps Flash out of the render path, and a last-known-good copy with
no expiry is what keeps an outage longer than the TTL from blanking a payment
method that was correct a minute ago.

One difference, and it is deliberate: an unreadable Flash with nothing stored
returns an empty map rather than raising. There is nothing for a caller to do
with the distinction — a token we cannot resolve is a payment method we do not
show, which is the same answer an ambiguous plan already gives.
"""

import asyncio
import json

from app.core.config import settings
from app.core.flash import (
    FlashUnavailable,
    fetch_settings_raw,
    parse_acceptance_methods,
)
from app.core.loggr import loggr
from app.core.redis_db import redis_client

logger = loggr.get_logger(__name__)

# Account-scoped, and we hold one account's key — so one entry, not one per id.
FRESH_KEY = "flash:acceptance-methods"
LAST_GOOD_KEY = "flash:acceptance-methods:lkg"

# One in-flight read, so a burst arriving on an expired key is one Flash call.
_read = asyncio.Lock()


async def read_acceptance_methods() -> dict[str, str]:
    """Every acceptance-method token on the account, and how each one pays."""
    if settings.flash_mock_enabled:
        # Ahead of the cache and never written to it, exactly as the plan cache
        # does: the dev endpoint that sets these would otherwise appear not to
        # work until the TTL ran out.
        from app.core import flash_mock

        return flash_mock.acceptance_methods()

    cached = await _load(FRESH_KEY)
    if cached is not None:
        return cached

    async with _read:
        cached = await _load(FRESH_KEY)
        if cached is not None:
            return cached
        return await _refresh()


async def _refresh() -> dict[str, str]:
    try:
        methods = parse_acceptance_methods(await fetch_settings_raw())
    except FlashUnavailable:
        last_good = await _load(LAST_GOOD_KEY)
        if last_good is None:
            logger.warning(
                "Flash is unreadable and no acceptance methods are cached; no "
                "payment method can be resolved until it answers again"
            )
            return {}
        logger.warning(
            "Flash is unreadable; serving the last known acceptance methods"
        )
        return last_good

    await _store(methods)
    return methods


async def _load(key: str) -> dict[str, str] | None:
    """Redis being down is a cache miss, never a failure."""
    try:
        stored = await redis_client.get(key)
    except Exception:
        logger.warning("Could not read %s from Redis", key, exc_info=True)
        return None
    if not stored:
        return None
    try:
        methods = json.loads(stored)
    except ValueError:
        return None
    return methods if isinstance(methods, dict) else None


async def _store(methods: dict[str, str]) -> None:
    payload = json.dumps(methods)
    try:
        # The plans' TTL rather than one of its own: these change even less
        # often than plans do, and a second knob would be a second thing to get
        # wrong for no behaviour anyone wants to tune separately.
        await redis_client.set(
            FRESH_KEY, payload, ex=settings.flash_plans_cache_ttl_seconds
        )
        await redis_client.set(LAST_GOOD_KEY, payload)
    except Exception:
        logger.warning("Could not cache Flash acceptance methods", exc_info=True)
