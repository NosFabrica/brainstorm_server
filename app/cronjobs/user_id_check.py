import asyncio

from app.core.config import settings
from app.core.loggr import loggr
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import count_nodes_without_user_id

logger = loggr.get_logger(__name__)


async def check_user_ids() -> int:
    """How many user nodes should have a `user_id` and do not; warns when not 0.
    Nodes with a malformed pubkey can never have one and are not counted."""
    async with neo4j_driver.session() as session:
        missing, malformed = await count_nodes_without_user_id(session)
    if missing:
        logger.warning(
            f"{missing} NostrUser nodes have no user_id "
            f"({malformed} more have a malformed pubkey): run scripts.backfill_user_ids"
        )
    return missing


async def user_id_check_cronjob():
    """A node without a `user_id` is invisible to Scorecard reads, and Neo4j
    Community cannot require the property, so count them on a schedule."""
    if not settings.user_ids_enabled:
        return
    logger.info("User id check cronjob started!")
    while True:
        try:
            await check_user_ids()
        except Exception as e:
            logger.error("User id check cronjob errored! " + str(e))
        await asyncio.sleep(settings.user_id_check_interval_hours * 3600)
