"""Redis leader-lock so exactly one server instance runs a given periodic job.

Single key set with SET NX PX; the holder renews it each cycle (SET XX PX). On
crash the key expires and another instance takes over on the next cycle.

Two jobs hold locks here, on separate keys. The key is required rather than
defaulted: a third holder that inherited one silently would contend with an
existing job, and one of the two would simply never run, with nothing to see.
"""

import socket
from uuid import uuid4

# Stable for the life of the process, so a holder can recognise its own lock.
INSTANCE_ID = f"{socket.gethostname()}:{uuid4()}"

SCHEDULER_LOCK_KEY = "lock:scheduler:leader"
BILLING_LOCK_KEY = "lock:billing:leader"


async def acquire_or_renew_leader(
    redis, instance_id: str, ttl_ms: int, *, key: str
) -> bool:
    """True if this instance holds `key` (freshly acquired or renewed)."""
    if await redis.set(key, instance_id, nx=True, px=ttl_ms):
        return True
    if await redis.get(key) == instance_id:
        await redis.set(key, instance_id, xx=True, px=ttl_ms)
        return True
    return False
