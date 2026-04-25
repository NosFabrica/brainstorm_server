from app.core.loggr import loggr
from neo4j import AsyncDriver as AsyncNeoDriver
import time
from tqdm import tqdm
from itertools import islice

BATCH_SIZE = 100  # Adjust as needed

logger = loggr.get_logger(__name__)


async def process_strfry_event(session: AsyncNeoDriver, event: dict):

    kind = event.get("kind")
    logger.info(f"processing event kind={kind} tags={len(event.get('tags', []))}")

    if kind == 3:
        # logger.info("Consuming event of kind 3")
        return await process_event_kind_3(session, event)

    if kind == 10000:
        # logger.info("Consuming event of kind 10000")
        return await process_event_kind_10000(session, event)

    if kind == 1984:
        # logger.info("Consuming event of kind 1984")
        return await process_event_kind_1984(session, event)


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


async def process_event_kind_10000(session: AsyncNeoDriver, event: dict):

    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    muted_pubkeys = [tag[1] for tag in event.get("tags", []) if tag[0] == "p"]

    # if not muted_pubkeys and event["content"]:
    #     return

    if not muted_pubkeys:
        cypher = """
        MATCH (pub:NostrUser {pubkey: $publisher})-[r:MUTES]->()
        DELETE r
        """
        await session.run(cypher, publisher=publisher)
        return

    cypher = """
    MERGE (pub:NostrUser {pubkey: $publisher})

    WITH pub, $muted_pubkeys AS fps
    UNWIND fps AS fp
        MERGE (f:NostrUser {pubkey: fp})
        MERGE (pub)-[:MUTES]->(f)

    WITH pub, fps
    OPTIONAL MATCH (pub)-[r:MUTES]->(oldF)
    WHERE NOT oldF.pubkey IN fps
    DELETE r"""

    await session.run(cypher, publisher=publisher, muted_pubkeys=muted_pubkeys)


async def process_event_kind_3(session: AsyncNeoDriver, event: dict):

    publisher = event["pubkey"]
    # Extract followed pubkeys from tags [["p","pubkey1"], ...]
    followed_pubkeys = [tag[1] for tag in event.get("tags", []) if tag[0] == "p"]

    if not followed_pubkeys:
        cypher = """
        MATCH (pub:NostrUser {pubkey: $publisher})-[r:FOLLOWS]->()
        DELETE r
        """
        await session.run(cypher, publisher=publisher)
        return

    cypher = """
    MERGE (pub:NostrUser {pubkey: $publisher})

    WITH pub, $followed_pubkeys AS fps
    UNWIND fps AS fp
        MERGE (f:NostrUser {pubkey: fp})
        MERGE (pub)-[:FOLLOWS]->(f)

    WITH pub, fps
    OPTIONAL MATCH (pub)-[r:FOLLOWS]->(oldF)
    WHERE NOT oldF.pubkey IN fps
    DELETE r"""

    await session.run(cypher, publisher=publisher, followed_pubkeys=followed_pubkeys)
