import asyncio
from datetime import timedelta

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.repos.support_repo import clear_expired_support_diagnostics_on_db

logger = loggr.get_logger(__name__)

# Deliberately not a setting: how long personal data is kept is a policy
# decision, not something an operator lengthens per deployment with an env var.
# It guards the policy, not the outcome — the cadence below *is* tunable, and
# an absurd interval lengthens retention in effect. That is drift to notice in
# review, not an attack to defend against.
RETENTION = timedelta(days=30)


async def expire_support_diagnostics_cronjob():
    """Drop support diagnostics once they are past the retention window.

    The rest of the database keeps everything; this is the one thing that
    expires, because it is the sharpest data support collects and the only
    part of a ticket with no value once the ticket is done.
    """
    logger.info("Support diagnostics expiry cronjob started!")
    while True:
        try:
            async with db_session() as db:
                cleared = await clear_expired_support_diagnostics_on_db(db, RETENTION)
            if cleared:
                logger.info(f"Cleared diagnostics on {cleared} support ticket(s)")
        except Exception as e:
            logger.error("Support diagnostics expiry cronjob errored! " + str(e))
        await asyncio.sleep(settings.support_diagnostics_sweep_interval_hours * 3600)
