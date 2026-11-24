"""Which naming of the Redis relationship sets may ingest.

A deploy that switches the naming runs two servers side by side: the new one
renames the sets while the old one would still write the old naming. The new
server claims its naming here before it renames; a consumer of the other naming
then stops taking events (they wait in the queue) for as long as the claim
stands: it resumes only if its own naming is claimed again.

`ingest:hold` stops every consumer, whatever naming, until an operator deletes
it; nothing here ever clears it.
"""
import asyncio

from app.core.loggr import loggr
from app.services.relationship_sets import names_are_ids

logger = loggr.get_logger(__name__)

NAMING_KEY = "migration:redis_sets:naming"
HOLD_KEY = "ingest:hold"
BUSY_PREFIX = "ingest:busy:"
# Longer than any one event takes; a consumer that died mid-event stops
# blocking a switch after this.
BUSY_TTL_SECONDS = 600


def _naming() -> str:
    return "ids" if names_are_ids() else "pubkeys"


async def may_ingest(redis_client) -> bool:
    held, claimed = await redis_client.mget(HOLD_KEY, NAMING_KEY)
    if held is not None:
        return False
    return claimed is None or claimed == _naming()


async def claim_naming(redis_client) -> None:
    """From now only this server's naming ingests. On a switch, returns once no
    consumer of the other naming is mid-event."""
    previous = await redis_client.getset(NAMING_KEY, _naming())
    if previous is None or previous == _naming():
        return
    logger.info(f"Redis set naming {previous} -> {_naming()}: ingest held.")
    while True:
        busy = [k async for k in redis_client.scan_iter(match=f"{BUSY_PREFIX}*")]
        if not busy:
            return
        await asyncio.sleep(0.5)
