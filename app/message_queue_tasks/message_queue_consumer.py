import asyncio
import json
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import get_redis_client
from app.db_models import BrainstormRequestStatus
from app.message_queue_tasks.process_strfry_event import (
    create_pubkey_index,
    process_strfry_event,
)
from app.message_queue_tasks.set_brainstorm_request_as_ongoing import (
    process_job_started_message,
)
from app.message_queue_tasks.upload_nostr_events import process_nostr_upload_message
from app.message_queue_tasks.write_neo4j_results import process_neo4j_write_message
from app.models.grapeRankResult import GrapeRankResult
from app.repos.brainstorm_nsec import update_last_time_calculated_graperank_on_db
from app.repos.brainstorm_request_repo import (
    update_brainstorm_request_result_by_id_on_db,
)
from app.neo4j_db.driver import driver as neo4j_driver

logger = loggr.get_logger(__name__)

RESULTS_QUEUE_NAME = "results_message_queue"
UPLOAD_NOSTR_RESULTS_QUEUE_NAME = "nostr_results_message_queue"
WRITE_NEO4J_RESULTS_QUEUE_NAME = "write_neo4j_message_queue"
STRFRY_EVENTS_QUEUE_NAME = "strfry:events"
JOB_STARTED_QUEUE_NAME = "job_started_queue"


async def process_message(message: dict):

    grape_rank_result = GrapeRankResult.model_validate(message["result"])

    status = (
        BrainstormRequestStatus.SUCCESS
        if grape_rank_result.success
        else BrainstormRequestStatus.FAILURE
    )

    pubkey: str | None = None
    if grape_rank_result.scorecards:
        pubkey = grape_rank_result.scorecards.popitem()[1].observer

    number_by_confidence_by_hops = {
        "high": {},
        "medium_high": {},
        "medium": {},
        "medium_low": {},
        "low": {},
        "low_and_reported_by_2_or_more_trusted_pubkeys": {},
    }

    if grape_rank_result.scorecards:
        for _, scorecard in grape_rank_result.scorecards.items():
            confidence = "high"
            if scorecard.influence < 0.5:
                confidence = "medium_high"
            if scorecard.influence < 0.2:
                confidence = "medium"
            if scorecard.influence < 0.07:
                confidence = "medium_low"
            if scorecard.influence < 0.02:
                if scorecard.trusted_reporters >= 2:
                    confidence = "low_and_reported_by_2_or_more_trusted_pubkeys"
                else:
                    confidence = "low"

            if not number_by_confidence_by_hops[confidence].get(scorecard.hops):
                number_by_confidence_by_hops[confidence][scorecard.hops] = 0

            number_by_confidence_by_hops[confidence][scorecard.hops] += 1

    async with db_session() as db:
        await update_brainstorm_request_result_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            result=json.dumps(message["result"]),
            status=status,
            count_values=json.dumps(number_by_confidence_by_hops),
        )
        if pubkey:
            await update_last_time_calculated_graperank_on_db(db, pubkey)
        await db.commit()


async def consume_messages():

    logger.info(
        f"Connected to Redis. Waiting for messages on '{RESULTS_QUEUE_NAME}'..."
    )

    while True:
        redis_client = None

        try:
            redis_client = get_redis_client()

            while True:
                msg = await redis_client.blpop(RESULTS_QUEUE_NAME, timeout=30)
                if msg:
                    try:
                        _, message_bytes = msg
                        message = json.loads(message_bytes)
                        # asyncio.create_task(process_message(message))
                        await process_message(message)
                    except Exception as e:
                        logger.error(e)

        except Exception as e:
            await asyncio.sleep(2)  # backoff

        finally:
            if redis_client:
                try:
                    await redis_client.close()
                except Exception:
                    pass


async def wait_until_graph_db_is_populated():
    while True:
        try:
            redis_client = get_redis_client()
            events_left = await redis_client.llen(STRFRY_EVENTS_QUEUE_NAME)
            if events_left < 500:
                return
            logger.info(f"Number of events left to process to neo4j: {events_left}")
            await asyncio.sleep(10)
        except Exception as e:
            print("error", e)


async def consume_strfry_plugin_messages():
    logger.info(
        f"Connected to Redis. Waiting for messages on '{STRFRY_EVENTS_QUEUE_NAME}'..."
    )

    while True:
        redis_client = None

        async with neo4j_driver.session() as neo4j_session:
            await create_pubkey_index(neo4j_session)
        try:
            redis_client = get_redis_client()
            while True:
                msg = await redis_client.blpop(STRFRY_EVENTS_QUEUE_NAME, timeout=30)
                if msg:
                    try:
                        _, message_bytes = msg
                        message = json.loads(message_bytes)

                        async with neo4j_driver.session() as neo4j_session:
                            await process_strfry_event(neo4j_session, message)

                    except Exception as e:
                        logger.error(e)

        except Exception as e:
            logger.error(f"exception {e}")
            await asyncio.sleep(2)  # backoff

        finally:
            if redis_client:
                try:
                    await redis_client.close()
                except Exception:
                    pass


async def consume_nostr_upload_messages():

    logger.info(
        f"Connected to Redis. Waiting for messages on '{UPLOAD_NOSTR_RESULTS_QUEUE_NAME}'..."
    )

    while True:
        redis_client = None

        try:
            redis_client = get_redis_client()

            while True:
                msg = await redis_client.blpop(
                    UPLOAD_NOSTR_RESULTS_QUEUE_NAME, timeout=30
                )
                if msg:
                    try:
                        _, message_bytes = msg
                        message = json.loads(message_bytes)
                        # asyncio.create_task(process_message(message))
                        await process_nostr_upload_message(message)
                    except Exception as e:
                        logger.error(e)

        except Exception as e:
            await asyncio.sleep(2)  # backoff

        finally:
            if redis_client:
                try:
                    await redis_client.close()
                except Exception:
                    pass


async def consume_neo4j_write_messages():
    logger.info(
        f"Connected to Redis. Waiting for messages on '{WRITE_NEO4J_RESULTS_QUEUE_NAME}'..."
    )

    while True:
        redis_client = None

        try:
            redis_client = get_redis_client()

            while True:
                msg = await redis_client.blpop(
                    WRITE_NEO4J_RESULTS_QUEUE_NAME, timeout=30
                )
                if msg:
                    try:
                        _, message_bytes = msg
                        message = json.loads(message_bytes)
                        # asyncio.create_task(process_message(message))
                        await process_neo4j_write_message(message)
                    except Exception as e:
                        logger.error(e)

        except Exception as e:
            await asyncio.sleep(2)  # backoff

        finally:
            if redis_client:
                try:
                    await redis_client.close()
                except Exception:
                    pass


async def consume_job_started_messages():

    logger.info(
        f"Connected to Redis. Waiting for messages on '{JOB_STARTED_QUEUE_NAME}'..."
    )

    while True:
        redis_client = None

        try:
            redis_client = get_redis_client()

            while True:
                msg = await redis_client.blpop(JOB_STARTED_QUEUE_NAME, timeout=30)
                if msg:
                    try:
                        _, message_bytes = msg
                        message = json.loads(message_bytes)
                        # asyncio.create_task(process_message(message))
                        await process_job_started_message(message)
                    except Exception as e:
                        logger.error(e)

        except Exception as e:
            await asyncio.sleep(2)  # backoff

        finally:
            if redis_client:
                try:
                    await redis_client.close()
                except Exception:
                    pass
