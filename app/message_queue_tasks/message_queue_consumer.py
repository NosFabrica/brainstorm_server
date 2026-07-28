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
from app.core.tier_thresholds import (
    DEFAULT_VERIFIED_THRESHOLD,
    TIER_NAMES,
    classify_tier,
)
from app.core.config import settings
from app.models.grapeRankResult import GrapeRankResult
from app.repos.brainstorm_nsec import update_last_time_calculated_graperank_on_db
from app.repos.observer_whitelist_repo import upsert_observer_whitelist_on_db
from app.repos.brainstorm_request_repo import (
    select_brainstorm_request_by_id_on_db,
    update_brainstorm_request_internal_publication_status_by_id_on_db,
    update_brainstorm_request_result_by_id_on_db,
    update_brainstorm_request_ta_status_by_id_on_db,
)
from app.schemas.graperank_schemas import GrapeRankPresetParams
from app.services.verified_cutoffs import build_cutoffs_from_params
from app.neo4j_db.driver import driver as neo4j_driver

logger = loggr.get_logger(__name__)

RESULTS_QUEUE_NAME = "results_message_queue"
UPLOAD_NOSTR_RESULTS_QUEUE_NAME = "nostr_results_message_queue"
WRITE_NEO4J_RESULTS_QUEUE_NAME = "write_neo4j_message_queue"
STRFRY_EVENTS_QUEUE_NAME = "strfry:events"
JOB_STARTED_QUEUE_NAME = "job_started_queue"


def verified_line_for_run(request_row) -> float:
    """The verified line THIS run computed under.

    `count_values` is history, so it buckets against the run's own snapshotted
    `graperank_params`, never the observer's preset as it stands today. A row
    with no snapshot (predating the column) or an unreadable one falls back to
    the baseline rather than failing the result write.
    """
    params = getattr(request_row, "graperank_params", None)
    if not params:
        return DEFAULT_VERIFIED_THRESHOLD
    try:
        return build_cutoffs_from_params(
            GrapeRankPresetParams(**params)
        ).verified_line
    except (TypeError, ValueError):
        logger.warning(
            "Unusable graperank_params snapshot on the run; "
            "bucketing count_values against the baseline line instead"
        )
        return DEFAULT_VERIFIED_THRESHOLD


def bucket_scorecards_by_confidence_and_hops(
    scorecards, verified_line: float
) -> tuple[dict[str, dict[int, int]], dict[str, float]]:
    """`(count_values, whitelist)` for one run's scorecards.

    Buckets by (tier, hops) through `classify_tier` — the same fallthrough the
    read endpoints apply in Cypher — so stored history and the observer's live
    /stats agree. The whitelist is gated on the separate *validity* cutoff, so
    it doesn't move with the preset.
    """
    number_by_confidence_by_hops: dict[str, dict[int, int]] = {
        name: {} for name in TIER_NAMES
    }
    whitelist: dict[str, float] = {}

    for scorecard in (scorecards or {}).values():
        confidence = classify_tier(
            scorecard.influence, scorecard.trusted_reporters, verified_line
        )
        by_hops = number_by_confidence_by_hops[confidence]
        by_hops[scorecard.hops] = by_hops.get(scorecard.hops, 0) + 1

        rounded_influence = round(scorecard.influence, 2)
        if rounded_influence >= settings.cutoff_of_valid_graperank_scores:
            whitelist[scorecard.observee] = rounded_influence

    return number_by_confidence_by_hops, whitelist


async def process_message(message: dict):

    grape_rank_result = GrapeRankResult.model_validate(message["result"])

    status = (
        BrainstormRequestStatus.SUCCESS
        if grape_rank_result.success
        else BrainstormRequestStatus.FAILURE
    )

    # Observer is constant across a run's scorecards; read it non-mutatingly
    # (do NOT popitem — that drops one scorecard from the loops below).
    pubkey: str | None = None
    if grape_rank_result.scorecards:
        pubkey = next(iter(grape_rank_result.scorecards.values())).observer

    async with db_session() as db:
        # The line THIS run used, so stored history and live /stats agree.
        request_row = await select_brainstorm_request_by_id_on_db(
            db, message["private_id"]
        )
        number_by_confidence_by_hops, whitelist = (
            bucket_scorecards_by_confidence_and_hops(
                grape_rank_result.scorecards,
                verified_line_for_run(request_row),
            )
        )

        await update_brainstorm_request_result_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            status=status,
            count_values=json.dumps(number_by_confidence_by_hops),
            error=grape_rank_result.error.model_dump() if grape_rank_result.error else None,
        )
        if status == BrainstormRequestStatus.FAILURE:
            # Calc failed -> the publish + neo4j-write stages never run; mark
            # them terminal so the row isn't forever "in pipeline".
            await update_brainstorm_request_ta_status_by_id_on_db(
                db,
                brainstorm_request_id=message["private_id"],
                status=BrainstormRequestStatus.FAILURE,
            )
            await update_brainstorm_request_internal_publication_status_by_id_on_db(
                db,
                brainstorm_request_id=message["private_id"],
                status=BrainstormRequestStatus.FAILURE,
            )
        # Persist the observer's whitelist snapshot, atomic with the status
        # write. Only on a successful calc with scorecards present: a degenerate
        # empty-scorecard run must NOT wipe a good snapshot. An all-below-cutoff
        # run legitimately writes {} (nobody trusted now).
        if (
            status == BrainstormRequestStatus.SUCCESS
            and grape_rank_result.scorecards
            and pubkey
        ):
            await upsert_observer_whitelist_on_db(
                db, pubkey, whitelist, message["private_id"]
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
