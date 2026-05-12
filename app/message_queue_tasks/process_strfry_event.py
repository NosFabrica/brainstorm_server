import json

from app.core.loggr import loggr
from app.core.meilisearch import NOSTR_PROFILES_INDEX, upsert_documents
from app.core.redis_db import redis_client
from neo4j import AsyncDriver as AsyncNeoDriver
import time
from tqdm import tqdm
from itertools import islice

BATCH_SIZE = 100  # Adjust as needed

FOLLOWED_BY_KEY_PREFIX = "followed_by:"
MUTED_BY_KEY_PREFIX = "muted_by:"
REPORTED_BY_KEY_PREFIX = "reported_by:"

KIND_0_PROFILE_FIELDS = (
    "name",
    "display_name",
    "about",
    "picture",
    "banner",
    "nip05",
    "lud06",
    "lud16",
    "website",
)

logger = loggr.get_logger(__name__)


async def process_strfry_event(session: AsyncNeoDriver, event: dict):

    kind = event.get("kind")

    if kind == 0:
        # logger.info("Consuming event of kind 0")
        return await process_event_kind_0(event)

    if kind == 3:
        # logger.info("Consuming event of kind 3")
        return await process_event_kind_3(session, event)

    if kind == 10000:
        # logger.info("Consuming event of kind 10000")
        return await process_event_kind_10000(session, event)

    if kind == 1984:
        # logger.info("Consuming event of kind 1984")
        return await process_event_kind_1984(session, event)


async def process_event_kind_0(event: dict):
    publisher = event["pubkey"]
    content_raw = event.get("content") or ""

    try:
        profile = json.loads(content_raw) if content_raw else {}
    except json.JSONDecodeError:
        return

    if not isinstance(profile, dict):
        return

    # Include every typical kind 0 field so a missing key in the new event
    # explicitly clears the previous value, while leaving non-kind-0 fields
    # on the existing Meilisearch document untouched (partial update).
    document = {"pubkey": publisher}
    for field in KIND_0_PROFILE_FIELDS:
        document[field] = profile.get(field)

    await upsert_documents(NOSTR_PROFILES_INDEX, [document])


async def create_pubkey_index(session: AsyncNeoDriver):
    query = """
    CREATE CONSTRAINT nostr_user_pubkey IF NOT EXISTS
    FOR (u:NostrUser)
    REQUIRE u.pubkey IS UNIQUE
    """

    await session.run(query)


async def process_event_kind_1984(session: AsyncNeoDriver, event: dict):

    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    reported_pubkeys = [tag[1] for tag in event.get("tags", []) if tag[0] == "p"]

    # if not reported_pubkeys and event["content"]:
    #     return

    if not reported_pubkeys:
        return

    cypher = """
    MERGE (pub:NostrUser {pubkey: $publisher})

    WITH pub, $reported_pubkeys AS rps
    UNWIND rps AS rp
        MERGE (reported:NostrUser {pubkey: rp})
        MERGE (pub)-[:REPORTS]->(reported)
    """

    await session.run(cypher, publisher=publisher, reported_pubkeys=reported_pubkeys)

    await _update_reverse_sets(
        REPORTED_BY_KEY_PREFIX, publisher, added_pubkeys=reported_pubkeys
    )


async def process_event_kind_10000(session: AsyncNeoDriver, event: dict):

    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    muted_pubkeys = [tag[1] for tag in event.get("tags", []) if tag[0] == "p"]

    # if not muted_pubkeys and event["content"]:
    #     return

    if not muted_pubkeys:
        cypher = """
        OPTIONAL MATCH (pub:NostrUser {pubkey: $publisher})-[r:MUTES]->(oldF)
        WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
        FOREACH (rel IN rels | DELETE rel)
        RETURN removed
        """
        result = await session.run(cypher, publisher=publisher)
        record = await result.single()
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
    await session.run(upsert_cypher, publisher=publisher, muted_pubkeys=muted_pubkeys)

    cleanup_cypher = """
    MATCH (pub:NostrUser {pubkey: $publisher})-[r:MUTES]->(oldF)
    WHERE NOT oldF.pubkey IN $muted_pubkeys
    WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
    FOREACH (rel IN rels | DELETE rel)
    RETURN removed
    """
    result = await session.run(
        cleanup_cypher, publisher=publisher, muted_pubkeys=muted_pubkeys
    )
    record = await result.single()
    removed = record["removed"] if record else []

    await _update_reverse_sets(
        MUTED_BY_KEY_PREFIX,
        publisher,
        added_pubkeys=muted_pubkeys,
        removed_pubkeys=removed,
    )


async def process_event_kind_3(session: AsyncNeoDriver, event: dict):

    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    followed_pubkeys = [tag[1] for tag in event.get("tags", []) if tag[0] == "p"]

    if not followed_pubkeys:
        cypher = """
        OPTIONAL MATCH (pub:NostrUser {pubkey: $publisher})-[r:FOLLOWS]->(oldF)
        WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
        FOREACH (rel IN rels | DELETE rel)
        RETURN removed
        """
        result = await session.run(cypher, publisher=publisher)
        record = await result.single()
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
    await session.run(
        upsert_cypher, publisher=publisher, followed_pubkeys=followed_pubkeys
    )

    cleanup_cypher = """
    MATCH (pub:NostrUser {pubkey: $publisher})-[r:FOLLOWS]->(oldF)
    WHERE NOT oldF.pubkey IN $followed_pubkeys
    WITH collect(oldF.pubkey) AS removed, collect(r) AS rels
    FOREACH (rel IN rels | DELETE rel)
    RETURN removed
    """
    result = await session.run(
        cleanup_cypher, publisher=publisher, followed_pubkeys=followed_pubkeys
    )
    record = await result.single()
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
