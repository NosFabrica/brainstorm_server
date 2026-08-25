"""Periodic billing reconciliation.

Two duties, in order of how much they can be trusted:

1. **Revoke what has provably lapsed** — locally decidable, no network.
2. **Re-read Flash for what isn't** — a `past_due` row, one still recorded
   `active` past its period end, or one we simply haven't asked about lately.
   Flash retries a failed delivery a few times and then never replays it, so
   this is the only path that recovers a lost webhook.

The durability slice adds replaying webhooks we acknowledged and then dropped,
pruning old payloads, and a leader lock. No lock yet is safe rather than lucky —
both duties are idempotent, so two replicas racing reach the same state.
"""

import asyncio
from datetime import timedelta

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.services.billing_sync_service import (
    reconcile_subscriptions,
    revoke_lapsed_entitlements,
)

logger = loggr.get_logger(__name__)


async def billing_sync_cronjob() -> None:
    if not settings.billing_sync_active:
        logger.info("Billing sync inactive; not starting")
        return

    while True:
        try:
            async with db_session() as db:
                revoked = await revoke_lapsed_entitlements(db)
                result = await reconcile_subscriptions(
                    db,
                    limit=settings.billing_reconcile_batch,
                    stale_after=timedelta(
                        seconds=settings.billing_reconcile_stale_after_seconds
                    ),
                )
            if revoked or result.reconciled or result.failed:
                logger.info(
                    "Billing sync: revoked %s, reconciled %s, failed %s",
                    revoked,
                    result.reconciled,
                    result.failed,
                )
        except Exception:
            # One bad cycle must not kill the loop — the next one retries.
            logger.exception("Billing sync cycle failed")
        await asyncio.sleep(settings.billing_sync_interval_seconds)
