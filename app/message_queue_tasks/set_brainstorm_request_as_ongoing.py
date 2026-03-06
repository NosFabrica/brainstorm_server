import asyncio
import json
from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import BrainstormRequestStatus

from app.repos.brainstorm_request_repo import (
    update_brainstorm_request_result_by_id_on_db,
)
from app.neo4j_db.driver import driver as neo4j_driver

logger = loggr.get_logger(__name__)

RESULTS_QUEUE_NAME = "results_message_queue"
UPLOAD_NOSTR_RESULTS_QUEUE_NAME = "nostr_results_message_queue"
WRITE_NEO4J_RESULTS_QUEUE_NAME = "write_neo4j_message_queue"
STRFRY_EVENTS_QUEUE_NAME = "strfry:events"


async def process_job_started_message(message: dict):

    request_id = message["id"]

    async with db_session() as db:
        await update_brainstorm_request_result_by_id_on_db(
            db,
            brainstorm_request_id=request_id,
            result="",
            status=BrainstormRequestStatus.ONGOING,
            count_values="",
        )
        await db.commit()
