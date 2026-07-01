"""Redis leader-lock so exactly one server instance schedules at a time.

Single key set with SET NX PX; the holder renews it each cycle (SET XX PX). On
crash the key expires and another instance takes over on the next cycle.
"""

SCHEDULER_LOCK_KEY = "lock:scheduler:leader"


async def acquire_or_renew_leader(redis, instance_id: str, ttl_ms: int) -> bool:
    """True if this instance is the scheduling leader (freshly acquired or renewed)."""
    if await redis.set(SCHEDULER_LOCK_KEY, instance_id, nx=True, px=ttl_ms):
        return True
    if await redis.get(SCHEDULER_LOCK_KEY) == instance_id:
        await redis.set(SCHEDULER_LOCK_KEY, instance_id, xx=True, px=ttl_ms)
        return True
    return False
