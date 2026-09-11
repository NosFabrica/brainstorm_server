"""Periodic billing reconciliation.

Four duties, in order of how much they can be trusted:

1. **Finish what we acknowledged and dropped** — a process killed between the
   200 and the entitlement write leaves a recorded event nothing acted on, and
   Flash will not send it again.
2. **Revoke what has provably lapsed** — locally decidable, no network.
3. **Re-read Flash for what is not** — a `past_due` row, one still recorded
   `active` past its period end, or one we haven't asked about lately.

Correctness does not depend on the leader lock: replay claims each event through
the database, and the other three are idempotent. The lock is there so N
replicas don't all hammer Flash's API for the same answers.
"""

import asyncio
from datetime import timedelta

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.services.billing_sync_service import (
    reconcile_subscriptions,
    replay_unprocessed_events,
    revoke_lapsed_entitlements,
)
from app.services.leader_lock import (
    BILLING_LOCK_KEY,
    INSTANCE_ID,
    acquire_or_renew_leader,
)

logger = loggr.get_logger(__name__)


async def billing_sync_cronjob() -> None:
    if not settings.billing_sync_active:
        logger.info("Billing sync inactive; not starting")
        return

    while True:
        try:
            # The lock has to outlive one cycle's work, not the gap between
            # cycles: a batch is bounded by billing_reconcile_batch round trips
            # to Flash, and an expiry mid-cycle lets a second replica start the
            # same work. Nothing renews it — at a six-hour interval there is no
            # second acquisition inside the window to renew from.
            ttl_ms = int(
                settings.billing_reconcile_batch
                * settings.flash_http_timeout_seconds
                * 4
                * 1000
            )
            if not await acquire_or_renew_leader(
                redis_client, INSTANCE_ID, ttl_ms, key=BILLING_LOCK_KEY
            ):
                await asyncio.sleep(settings.billing_sync_interval_seconds)
                continue

            async with db_session() as db:
                replayed = await replay_unprocessed_events(
                    db,
                    limit=settings.billing_replay_batch,
                    stale_after=timedelta(
                        seconds=settings.billing_replay_stale_after_seconds
                    ),
                    max_attempts=settings.billing_replay_max_attempts,
                )
                revoked = await revoke_lapsed_entitlements(db)
                result = await reconcile_subscriptions(
                    db,
                    limit=settings.billing_reconcile_batch,
                    stale_after=timedelta(
                        seconds=settings.billing_reconcile_stale_after_seconds
                    ),
                    abandon_pending_after=timedelta(
                        seconds=settings.billing_abandon_pending_after_seconds
                    ),
                )
            if replayed or revoked or result.reconciled or result.failed:
                logger.info(
                    "Billing sync: replayed %s, revoked %s, reconciled %s, failed %s",
                    replayed,
                    revoked,
                    result.reconciled,
                    result.failed,
                )
        except Exception:
            # One bad cycle must not kill the loop — the next one retries.
            logger.exception("Billing sync cycle failed")
        await asyncio.sleep(settings.billing_sync_interval_seconds)
