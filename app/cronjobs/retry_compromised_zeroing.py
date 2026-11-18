import asyncio
from datetime import timedelta

from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.services.compromised_service import retry_due_zeroings
from app.services.leader_lock import (
    COMPROMISED_LOCK_KEY,
    INSTANCE_ID,
    acquire_or_renew_leader,
)

logger = loggr.get_logger(__name__)

INTERVAL_SECONDS = 60
# A zeroing that hasn't reported this long after its account changed died with
# its pod (PUT waits for it, and a run takes seconds).
PENDING_GRACE = timedelta(minutes=2)
# A failed or incomplete zeroing (relay down, an unacked TA) is tried again
# this long after it ran.
RETRY_AFTER = timedelta(minutes=10)
# Renewed every cycle; a held lock outlives a slow cycle.
LOCK_TTL_MS = 5 * INTERVAL_SECONDS * 1000


async def retry_compromised_zeroing_cronjob():
    """Make sure every compromised account's zeroing lands: re-run the ones that
    never reported (the run started by PUT died with its pod) or didn't succeed
    (app/services/compromised_service.py). One instance at a time."""
    logger.info("Compromised zeroing retry cronjob started!")
    while True:
        try:
            if await acquire_or_renew_leader(
                redis_client, INSTANCE_ID, LOCK_TTL_MS, key=COMPROMISED_LOCK_KEY
            ):
                retried = await retry_due_zeroings(PENDING_GRACE, RETRY_AFTER)
                if retried:
                    logger.info(f"Re-ran {retried} compromised zeroing(s)")
        except Exception as e:
            logger.error("Compromised zeroing retry cronjob errored! " + str(e))
        await asyncio.sleep(INTERVAL_SECONDS)
