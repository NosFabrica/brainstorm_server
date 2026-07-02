import json

from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.core.vespa import PROFILE_FIELDS as KIND_0_PROFILE_FIELDS
from app.core.vespa import upsert_profile
from neo4j import AsyncDriver as AsyncNeoDriver
import time
from tqdm import tqdm
from itertools import islice

BATCH_SIZE = 100  # Adjust as needed

FOLLOWED_BY_KEY_PREFIX = "followed_by:"
MUTED_BY_KEY_PREFIX = "muted_by:"
REPORTED_BY_KEY_PREFIX = "reported_by:"

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


# Canonical kind-0 field -> candidate keys in PRIORITY order (canonical first,
# deprecated aliases after). We store ONLY the canonical field, picking the first
# candidate that carries a non-empty value — so a deprecated key merely backfills
# when the canonical is missing/blank. Per NIP-24, `username` is a deprecated
# alias of `name` and `displayName` of `display_name`; some clients also send
# these as kind-0 *tags* rather than in `content`. See docs/search-vs-tapestry.md
# §8.4.1. Keys must be the canonical PROFILE_FIELDS (KIND_0_PROFILE_FIELDS).
_FIELD_RESOLUTION: dict[str, tuple[str, ...]] = {
    "name": ("name", "username"),            # NIP-24: username deprecated -> name
    "display_name": ("display_name", "displayName"),
    "about": ("about",),
    "picture": ("picture",),
    "banner": ("banner",),
    "nip05": ("nip05",),
    "lud06": ("lud06",),
    "lud16": ("lud16",),
    "website": ("website",),
}

# Guard against drift between the resolution table and the Vespa schema fields.
assert set(_FIELD_RESOLUTION) == set(KIND_0_PROFILE_FIELDS), (
    "FIELD_RESOLUTION keys must match vespa.PROFILE_FIELDS"
)


def _extract_kind0_profile(event: dict) -> dict:
    """Resolve each canonical profile field from a kind-0 event's `content` JSON
    and profile `tags`.

    Two-level precedence:
      * source — `content` is the base; profile `tags` only fill keys content
        didn't provide (content wins on conflict).
      * candidate — for each canonical field, the first key in
        ``_FIELD_RESOLUTION`` that carries a non-empty value wins, so a
        deprecated alias (`username`, `displayName`) only backfills when the
        canonical key is absent/blank.

    Only canonical ``PROFILE_FIELDS`` are returned; deprecated keys are never
    surfaced as their own field.
    """
    merged: dict = {}

    content_raw = event.get("content") or ""
    if content_raw:
        try:
            parsed = json.loads(content_raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            merged.update(parsed)

    for tag in event.get("tags") or []:
        if isinstance(tag, list) and len(tag) >= 2 and isinstance(tag[0], str):
            key, value = tag[0], tag[1]
            # Tags only FILL gaps — content (above) wins on conflict.
            if key not in merged and isinstance(value, str):
                merged[key] = value

    out: dict = {}
    for field, candidates in _FIELD_RESOLUTION.items():
        for key in candidates:
            value = merged.get(key)
            if isinstance(value, str) and value.strip():
                out[field] = value
                break
    return out


async def process_event_kind_0(event: dict):
    publisher = event["pubkey"]
    profile = _extract_kind0_profile(event)

    # Skip a kind-0 carrying NO recognized profile fields (empty/malformed): the
    # upsert clears missing fields to "", so an empty event would wipe an
    # existing good profile. See docs/search-vs-tapestry.md §8.4.1.
    if not profile:
        return

    # Vespa partial update: every standard kind-0 field gets assigned (or
    # cleared if missing from the new event), leaving non-kind-0 attributes
    # like quality_scores on the existing document untouched. create=true
    # handles the new-pubkey case.
    await upsert_profile(pubkey=publisher, profile=profile)


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
