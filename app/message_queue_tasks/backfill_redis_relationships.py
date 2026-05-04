from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import get_redis_client
from app.message_queue_tasks.process_strfry_event import (
    FOLLOWED_BY_KEY_PREFIX,
    MUTED_BY_KEY_PREFIX,
    REPORTED_BY_KEY_PREFIX,
)
from app.neo4j_db.driver import driver as neo4j_driver
from app.nostr_event_transferer.nostr_event_transferer import ev_kinds
from app.repos.brainstorm_nostr_transferer import (
    get_nostr_transfer_status_by_kind_from_db,
)

logger = loggr.get_logger(__name__)

PAGE_SIZE = 2000
DONE_MARKER_KEY = "migration:redis_backfill:done"

_RELATIONSHIP_TO_PREFIX = {
    "FOLLOWS": FOLLOWED_BY_KEY_PREFIX,
    "MUTES": MUTED_BY_KEY_PREFIX,
    "REPORTS": REPORTED_BY_KEY_PREFIX,
}


async def _is_graph_db_populated() -> bool:
    async with db_session() as db:
        for kind, _ in ev_kinds:
            status = await get_nostr_transfer_status_by_kind_from_db(
                db, kind=kind.as_u16()
            )
            if not status or not status.completed:
                return False
    return True


async def _redis_has_relationship_data(redis_client) -> bool:
    for prefix in _RELATIONSHIP_TO_PREFIX.values():
        async for _ in redis_client.scan_iter(match=f"{prefix}*", count=1):
            return True
    return False


async def _backfill_relationship(redis_client, relationship: str, prefix: str) -> int:
    # Each page is a fresh, short-lived neo4j transaction over a contiguous slice
    # of NostrUser nodes ordered by pubkey (indexed via the unique constraint),
    # so memory and transaction lifetime stay bounded regardless of total size.
    cypher = f"""
    MATCH (target:NostrUser)
    WHERE target.pubkey > $cursor
    WITH target ORDER BY target.pubkey LIMIT $page
    OPTIONAL MATCH (source:NostrUser)-[:{relationship}]->(target)
    RETURN target.pubkey AS target, collect(source.pubkey) AS sources
    """
    cursor = ""
    users_seen = 0
    keys_written = 0
    pages_done = 0
    while True:
        async with neo4j_driver.session() as neo4j_session:
            result = await neo4j_session.run(
                cypher, cursor=cursor, page=PAGE_SIZE
            )
            records = await result.data()
        if not records:
            break

        pipe = redis_client.pipeline(transaction=False)
        page_keys = 0
        for record in records:
            sources = record["sources"]
            if sources:
                pipe.sadd(f"{prefix}{record['target']}", *sources)
                page_keys += 1
        if page_keys:
            await pipe.execute()

        users_seen += len(records)
        keys_written += page_keys
        cursor = max(r["target"] for r in records)
        pages_done += 1
        if pages_done % 10 == 0:
            logger.info(
                f"  {relationship}: {users_seen} users scanned, "
                f"{keys_written} target sets written."
            )

    logger.info(
        f"{relationship}: finished. {users_seen} users scanned, "
        f"{keys_written} target sets written."
    )
    return keys_written


async def backfill_redis_relationships_if_needed() -> None:
    redis_client = get_redis_client()
    try:
        if await redis_client.exists(DONE_MARKER_KEY):
            logger.info(
                f"Redis backfill marker '{DONE_MARKER_KEY}' present — skipping."
            )
            return

        if not await _is_graph_db_populated():
            logger.info(
                "Graph DB not populated yet — skipping Redis relationship backfill."
            )
            return

        if await _redis_has_relationship_data(redis_client):
            # No done-marker but prefix keys exist: a prior run left partial state.
            # Operator is expected to clear those keys manually before re-running.
            logger.warning(
                "Redis has relationship-prefix keys but no done marker — "
                "skipping. Clear those keys manually to re-run the backfill."
            )
            return

        logger.info(
            "Graph DB populated and Redis empty — starting one-shot backfill "
            f"from Neo4j (page size {PAGE_SIZE} users)."
        )
        for relationship, prefix in _RELATIONSHIP_TO_PREFIX.items():
            await _backfill_relationship(redis_client, relationship, prefix)

        await redis_client.set(DONE_MARKER_KEY, "1")
        logger.info(
            f"Redis relationship backfill complete; wrote marker '{DONE_MARKER_KEY}'."
        )
    finally:
        try:
            await redis_client.close()
        except Exception:
            pass
