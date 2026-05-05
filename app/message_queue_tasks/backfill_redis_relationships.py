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
PIPELINE_FLUSH_EVERY = 5000
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


async def _count_nostr_users() -> int:
    # Fast: neo4j keeps a label-count store, so this is effectively O(1).
    async with neo4j_driver.session() as neo4j_session:
        result = await neo4j_session.run(
            "MATCH (n:NostrUser) RETURN count(n) AS total"
        )
        record = await result.single()
        return int(record["total"]) if record else 0


async def _backfill_relationship(
    redis_client, relationship: str, prefix: str, total_users: int
) -> int:
    # We iterate by source (publisher) rather than by target. Out-degree is bounded
    # by a user's contact-list / mute-list / report-list size (usually thousands at
    # most), whereas in-degree can run into the millions for popular pubkeys —
    # collecting incoming edges per target would force pathological list sizes.
    # Pages are a fresh, short-lived neo4j transaction over a contiguous slice of
    # NostrUser nodes ordered by pubkey (indexed via the unique constraint).
    cypher = f"""
    MATCH (source:NostrUser)
    WHERE source.pubkey > $cursor
    WITH source ORDER BY source.pubkey LIMIT $page
    OPTIONAL MATCH (source)-[:{relationship}]->(target:NostrUser)
    RETURN source.pubkey AS source, collect(target.pubkey) AS targets
    """
    cursor = ""
    sources_seen = 0
    edges_written = 0
    pages_done = 0
    while True:
        page_max_cursor: str | None = None
        page_count = 0
        pipe = redis_client.pipeline(transaction=False)
        pending = 0

        async with neo4j_driver.session() as neo4j_session:
            result = await neo4j_session.run(
                cypher, cursor=cursor, page=PAGE_SIZE
            )
            async for record in result:
                source = record["source"]
                for target in record["targets"]:
                    pipe.sadd(f"{prefix}{target}", source)
                    pending += 1
                    if pending >= PIPELINE_FLUSH_EVERY:
                        await pipe.execute()
                        pipe = redis_client.pipeline(transaction=False)
                        edges_written += pending
                        pending = 0
                if page_max_cursor is None or source > page_max_cursor:
                    page_max_cursor = source
                page_count += 1

        if pending:
            await pipe.execute()
            edges_written += pending

        if page_count == 0:
            break

        sources_seen += page_count
        cursor = page_max_cursor  # type: ignore[assignment]
        pages_done += 1
        if pages_done % 10 == 0:
            pct = (sources_seen / total_users * 100) if total_users else 0.0
            remaining = max(total_users - sources_seen, 0)
            logger.info(
                f"  {relationship}: {sources_seen}/{total_users} sources "
                f"({pct:.2f}% done, {remaining} remaining), "
                f"{edges_written} edges written."
            )

    pct = (sources_seen / total_users * 100) if total_users else 0.0
    logger.info(
        f"{relationship}: finished. {sources_seen}/{total_users} sources "
        f"({pct:.2f}%), {edges_written} edges written."
    )
    return edges_written


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

        total_users = await _count_nostr_users()
        logger.info(
            "Graph DB populated and Redis empty — starting one-shot backfill "
            f"from Neo4j: {total_users} NostrUser nodes, page size {PAGE_SIZE}."
        )
        for relationship, prefix in _RELATIONSHIP_TO_PREFIX.items():
            await _backfill_relationship(
                redis_client, relationship, prefix, total_users
            )

        await redis_client.set(DONE_MARKER_KEY, "1")
        logger.info(
            f"Redis relationship backfill complete; wrote marker '{DONE_MARKER_KEY}'."
        )
    finally:
        try:
            await redis_client.close()
        except Exception:
            pass
