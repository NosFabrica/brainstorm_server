import asyncio
from datetime import timedelta

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.repos.brainstorm_request_repo import (
    fail_stale_ongoing_brainstorm_requests_on_db,
)

logger = loggr.get_logger(__name__)


async def fail_stale_ongoing_brainstorm_requests_cronjob():
    stale_threshold = timedelta(
        hours=settings.stale_ongoing_brainstorm_request_threshold_hours
    )
    check_interval_seconds = (
        settings.stale_ongoing_brainstorm_request_check_interval_minutes * 60
    )
    logger.info("Stale ONGOING brainstorm request cronjob started!")
    while True:
        try:
            async with db_session() as db:
                updated_count = await fail_stale_ongoing_brainstorm_requests_on_db(
                    db, stale_threshold
                )
            if updated_count:
                logger.info(
                    f"Marked {updated_count} stale ONGOING brainstorm request(s) as FAILURE"
                )
        except Exception as e:
            logger.error("Stale ONGOING brainstorm request cronjob errored! " + str(e))
        await asyncio.sleep(check_interval_seconds)
