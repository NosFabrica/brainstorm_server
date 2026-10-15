from neo4j import AsyncSession as AsyncNeoSession

from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.repos.tagging_repo import upsert_tag_element_on_db, upsert_user_tagging_on_db
from app.services.report_graph_service import (
    diff_author_targets,
    extract_report_targets,
    surviving_report_targets,
)
from app.services.report_relay_service import fetch_author_user_reports
from app.services.tagging_parse import (
    TAGGING_KIND,
    parse_tag_element,
    parse_user_tagging,
)
from app.utils.nostr import p_tag_pubkeys

BATCH_SIZE = 100  # Adjust as needed

FOLLOWED_BY_KEY_PREFIX = "followed_by:"
MUTED_BY_KEY_PREFIX = "muted_by:"
REPORTED_BY_KEY_PREFIX = "reported_by:"

logger = loggr.get_logger(__name__)


async def _write(session: AsyncNeoSession, cypher: str, **params):
    """Run one write query as a managed transaction, so the driver retries it on transient
    errors such as `DeadlockDetected` (the GrapeRank write-back locks the same nodes).
    """

    async def work(tx):
        result = await tx.run(cypher, **params)
        return await result.single()

    return await session.execute_write(work)


async def process_strfry_event(session: AsyncNeoSession, event: dict):
    kind = event.get("kind")

    if kind == 3:
        # logger.info("Consuming event of kind 3")
        return await process_event_kind_3(session, event)

    if kind == 10000:
        # logger.info("Consuming event of kind 10000")
        return await process_event_kind_10000(session, event)

    if kind == 1984:
        # logger.info("Consuming event of kind 1984")
        return await process_event_kind_1984(session, event)

    if kind == 5:
        # logger.info("Consuming event of kind 5")
        return await process_event_kind_5(session, event)

    if kind == TAGGING_KIND:
        # Tag elements and taggings are both kind 39999, told apart by `z`.
        return await process_event_kind_39999(event)


async def create_pubkey_index(session: AsyncNeoSession):
    query = """
    CREATE CONSTRAINT nostr_user_pubkey IF NOT EXISTS
    FOR (u:NostrUser)
    REQUIRE u.pubkey IS UNIQUE
    """

    await session.run(query)


async def process_event_kind_1984(session: AsyncNeoSession, event: dict):
    publisher = event["pubkey"]
    reported_pubkeys = extract_report_targets(event)

    if not reported_pubkeys:
        return

    cypher = """
    MERGE (pub:NostrUser {pubkey: $publisher})

    WITH pub, $reported_pubkeys AS rps
    UNWIND rps AS rp
        MERGE (reported:NostrUser {pubkey: rp})
        MERGE (pub)-[:REPORTS]->(reported)
    """

    await _write(
        session, cypher, publisher=publisher, reported_pubkeys=reported_pubkeys
    )

    await _update_reverse_sets(
        REPORTED_BY_KEY_PREFIX, publisher, added_pubkeys=reported_pubkeys
    )


async def process_event_kind_10000(session: AsyncNeoSession, event: dict):
    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    muted_pubkeys = p_tag_pubkeys(event.get("tags", []))

    # if not muted_pubkeys and event["content"]:
    #     return

    if not muted_pubkeys:
        cypher = """
        OPTIONAL MATCH (pub:NostrUser {pubkey: $publisher})-[r:MUTES]->(oldF)
        WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
        FOREACH (rel IN rels | DELETE rel)
        RETURN removed
        """
        record = await _write(session, cypher, publisher=publisher)
        removed = record["removed"] if record else []
        await _update_reverse_sets(
            MUTED_BY_KEY_PREFIX, publisher, removed_pubkeys=removed
        )
        return

    upsert_cypher = """
    MERGE (pub:NostrUser {pubkey: $publisher})

    WITH pub
    UNWIND $muted_pubkeys AS fp
        MERGE (f:NostrUser {pubkey: fp})
        MERGE (pub)-[:MUTES]->(f)
    """
    await _write(
        session, upsert_cypher, publisher=publisher, muted_pubkeys=muted_pubkeys
    )

    cleanup_cypher = """
    MATCH (pub:NostrUser {pubkey: $publisher})-[r:MUTES]->(oldF)
    WHERE NOT oldF.pubkey IN $muted_pubkeys
    WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
    FOREACH (rel IN rels | DELETE rel)
    RETURN removed
    """
    record = await _write(
        session, cleanup_cypher, publisher=publisher, muted_pubkeys=muted_pubkeys
    )
    removed = record["removed"] if record else []

    await _update_reverse_sets(
        MUTED_BY_KEY_PREFIX,
        publisher,
        added_pubkeys=muted_pubkeys,
        removed_pubkeys=removed,
    )


async def process_event_kind_3(session: AsyncNeoSession, event: dict):
    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    followed_pubkeys = p_tag_pubkeys(event.get("tags", []))

    if not followed_pubkeys:
        cypher = """
        OPTIONAL MATCH (pub:NostrUser {pubkey: $publisher})-[r:FOLLOWS]->(oldF)
        WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
        FOREACH (rel IN rels | DELETE rel)
        RETURN removed
        """
        record = await _write(session, cypher, publisher=publisher)
        removed = record["removed"] if record else []
        await _update_reverse_sets(
            FOLLOWED_BY_KEY_PREFIX, publisher, removed_pubkeys=removed
        )
        return

    upsert_cypher = """
    MERGE (pub:NostrUser {pubkey: $publisher})

    WITH pub
    UNWIND $followed_pubkeys AS fp
        MERGE (f:NostrUser {pubkey: fp})
        MERGE (pub)-[:FOLLOWS]->(f)
    """
    await _write(
        session, upsert_cypher, publisher=publisher, followed_pubkeys=followed_pubkeys
    )

    cleanup_cypher = """
    MATCH (pub:NostrUser {pubkey: $publisher})-[r:FOLLOWS]->(oldF)
    WHERE NOT oldF.pubkey IN $followed_pubkeys
    WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
    FOREACH (rel IN rels | DELETE rel)
    RETURN removed
    """
    record = await _write(
        session, cleanup_cypher, publisher=publisher, followed_pubkeys=followed_pubkeys
    )
    removed = record["removed"] if record else []

    await _update_reverse_sets(
        FOLLOWED_BY_KEY_PREFIX,
        publisher,
        added_pubkeys=followed_pubkeys,
        removed_pubkeys=removed,
    )


async def _update_reverse_sets(
    key_prefix: str,
    publisher: str,
    added_pubkeys: list[str] | None = None,
    removed_pubkeys: list[str] | None = None,
):
    added_pubkeys = added_pubkeys or []
    removed_pubkeys = removed_pubkeys or []
    if not added_pubkeys and not removed_pubkeys:
        return
    pipe = redis_client.pipeline(transaction=False)
    for pk in added_pubkeys:
        pipe.sadd(f"{key_prefix}{pk}", publisher)
    for pk in removed_pubkeys:
        pipe.srem(f"{key_prefix}{pk}", publisher)
    await pipe.execute()


async def _current_report_targets(session: AsyncNeoSession, author: str) -> set[str]:
    cypher = """
    OPTIONAL MATCH (pub:NostrUser {pubkey: $author})-[:REPORTS]->(t:NostrUser)
    RETURN collect(t.pubkey) AS targets
    """
    result = await session.run(cypher, author=author)
    record = await result.single()
    return set(record["targets"]) if record else set()


async def process_event_kind_5(session: AsyncNeoSession, event: dict):
    """NIP-09 deletion: reconcile the author's report edges against the relay.

    strfry purges the retracted 1984 before this kind-5 reaches us and the purge
    emits no message, so we recompute from what the author *still* reports rather
    than applying the deletion. See app/message_queue_tasks/CLAUDE.md.
    """
    author = event["pubkey"]

    # Cheap indexed read before the network round-trip: hold no report edges for
    # this author and their deletion can't remove anything. Only 0.76% of users
    # have any, so this is what keeps recomputes rare.
    current = await _current_report_targets(session, author)
    if not current:
        return

    reports = await fetch_author_user_reports(author)
    if reports is None:
        return  # relay unreadable/truncated: leave the graph alone (see service)

    desired = surviving_report_targets(reports, author)
    diff = diff_author_targets(desired, current)

    if diff.to_remove:
        await _write(
            session,
            """
            MATCH (pub:NostrUser {pubkey: $author})-[r:REPORTS]->(t:NostrUser)
            WHERE t.pubkey IN $targets
            DELETE r
            """,
            author=author,
            targets=sorted(diff.to_remove),
        )

    if diff.to_add:
        await _write(
            session,
            """
            MERGE (pub:NostrUser {pubkey: $author})
            WITH pub
            UNWIND $targets AS tp
                MERGE (t:NostrUser {pubkey: tp})
                MERGE (pub)-[:REPORTS]->(t)
            """,
            author=author,
            targets=sorted(diff.to_add),
        )

    # Re-assert the whole surviving set rather than only the Neo4j delta: Redis is
    # what GrapeRank reads and it drifts from Neo4j independently (the backfill
    # writes Neo4j, then Redis, non-atomically). SADD is idempotent, and we only
    # get here for the 0.76% of authors who report at all. Members Neo4j never
    # knew about are the backfill's job.
    await _update_reverse_sets(
        REPORTED_BY_KEY_PREFIX,
        author,
        added_pubkeys=sorted(desired),
        removed_pubkeys=sorted(diff.to_remove),
    )

    if diff:
        logger.info(
            "kind-5 report recompute for %s: +%d/-%d targets",
            author,
            len(diff.to_add),
            len(diff.to_remove),
        )


async def process_event_kind_39999(event: dict):
    """Persist a kind-39999 tag element or tagging.

    Both shapes ride the same kind and are distinguished by their `z` tag; an
    event matching neither concept is foreign traffic and is dropped silently
    (kind 39999 is a general Decentralized-Lists item kind, so unrelated events
    legitimately arrive here). Malformed events are dropped the same way — a bad
    event must never stall the queue for the good ones behind it.
    """
    element = parse_tag_element(event)
    tagging = parse_user_tagging(event)
    if element is None and tagging is None:
        return

    async with db_session() as db:
        if element is not None:
            await upsert_tag_element_on_db(db, element)
        elif tagging is not None:
            await upsert_user_tagging_on_db(db, tagging)
