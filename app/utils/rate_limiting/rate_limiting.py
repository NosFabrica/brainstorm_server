from fastapi import HTTPException

from app.core.redis_db import get_redis_client


RATE_LIMIT = 3
WINDOW_SECONDS = 1800  # 30 minutes


async def _enforce_window(key: str, limit: int, window_seconds: int) -> None:
    redis_client = get_redis_client()
    current = await redis_client.incr(key)
    if current == 1:
        await redis_client.expire(key, window_seconds)
    if current > limit:
        raise HTTPException(status_code=429, detail="Too many requests")


async def validateIfRequestedTooOftenByIP(ip_address: str) -> None:
    await _enforce_window(f"rate_limit:{ip_address}", RATE_LIMIT, WINDOW_SECONDS)


# Generous enough for the pending-checkout poll (every few seconds), tight
# enough that nobody hammers Flash on our credentials through us.
REFRESH_RATE_LIMIT = 12
REFRESH_WINDOW_SECONDS = 60


async def validate_subscription_refresh_allowed(pubkey: str) -> None:
    await _enforce_window(
        f"rate_limit:billing_refresh:{pubkey}",
        REFRESH_RATE_LIMIT,
        REFRESH_WINDOW_SECONDS,
    )


# An operator clicking through a report, not a poll — and every click spends our
# Flash quota on our credentials, so a stuck menu can't become an incident.
FLASH_RECORD_RATE_LIMIT = 30
FLASH_RECORD_WINDOW_SECONDS = 60


async def validate_flash_record_read_allowed(operator_pubkey: str) -> None:
    await _enforce_window(
        f"rate_limit:billing_flash_record:{operator_pubkey}",
        FLASH_RECORD_RATE_LIMIT,
        FLASH_RECORD_WINDOW_SECONDS,
    )
