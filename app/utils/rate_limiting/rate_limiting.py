from fastapi import HTTPException

from app.core.redis_db import get_redis_client


RATE_LIMIT = 3
WINDOW_SECONDS = 1800  # 30 minutes


async def validateIfRequestedTooOftenByIP(ip_address: str) -> None:

    redis_client = get_redis_client()
    key = f"rate_limit:{ip_address}"

    current = await redis_client.incr(key)

    if current == 1:
        await redis_client.expire(key, WINDOW_SECONDS)

    if current > RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
