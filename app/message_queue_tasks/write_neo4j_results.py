from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import BrainstormRequestStatus
from app.models.grapeRankResult import GrapeRankResult
from app.neo4j_db.driver import driver as neo4j_driver

import time
from tqdm import tqdm
from itertools import islice

from app.repos.brainstorm_request_repo import (
    update_brainstorm_request_internal_publication_status_by_id_on_db,
    update_brainstorm_request_status_by_id_on_db,
)

BATCH_SIZE = 100  # Adjust as needed

logger = loggr.get_logger(__name__)


async def process_neo4j_write_message(message: dict):
    is_success = message["result"]["success"]
    logger.info("neo4j write")
    logger.info(message["result"]["success"])
    # if not is_success:
    #     return

    logger.info("Writing results to Neo4j...")
    grape_rank_result = GrapeRankResult.model_validate(message["result"])
    if not grape_rank_result.scorecards:
        return

    observer = next(iter(grape_rank_result.scorecards.values())).observer
    scorecards = [x.model_dump() for x in grape_rank_result.scorecards.values()]

    async with db_session() as db:
        await update_brainstorm_request_internal_publication_status_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            status=BrainstormRequestStatus.ONGOING,
        )

        await db.commit()

    async def process_batch(batch):
        query = f"""
        UNWIND $rows AS row
        MATCH (n:NostrUser {{pubkey: row.observee}})
        SET n.influence_{observer} = row.influence,
            n.hops_{observer} = row.hops,
            n.trusted_reporters_{observer} = row.trusted_reporters
        """
        async with neo4j_driver.session() as session:
            await session.run(query, rows=batch)

    start_time = time.time()

    for i in tqdm(
        range(0, len(scorecards), BATCH_SIZE), desc="Processing Neo4j batches"
    ):
        batch = scorecards[i : i + BATCH_SIZE]
        await process_batch(batch=batch)

    async with db_session() as db:
        await update_brainstorm_request_internal_publication_status_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            status=BrainstormRequestStatus.SUCCESS,
        )

        await db.commit()

    final_time = time.time() - start_time
    logger.info(
        f"Took {final_time:.2f} seconds to process {len(scorecards)} Neo4j writes"
    )
    example_scorecard = next(islice(grape_rank_result.scorecards.values(), 1, 2))

    logger.info(f"Check the observed pubkey {example_scorecard.observee}")
