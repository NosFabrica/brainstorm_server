from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import get_redis_client
from app.message_queue_tasks.process_strfry_event import (
    DESIGNATES_KEY_PREFIX,
    FOLLOWED_BY_KEY_PREFIX,
    MUTED_BY_KEY_PREFIX,
    REPORTED_BY_KEY_PREFIX,
)
from app.neo4j_db.driver import driver as neo4j_driver
from app.nostr_event_transferer.nostr_event_transferer import ev_kinds
from app.repos.brainstorm_nostr_transferer import (
    get_nostr_transfer_status_by_kind_from_db,
)
from app.repos.user_id_repo import ids_for, pubkeys_for
from app.services.relationship_sets import is_set_name, names_are_ids

logger = loggr.get_logger(__name__)

PAGE_SIZE = 2000
PIPELINE_FLUSH_EVERY = 5000
DONE_MARKER_KEY = "migration:redis_backfill:done"
# The sets name users by pubkey, or by User id once scores are in Postgres
# (services/relationship_sets.py). Only the naming in use is kept current, so
# each has its own marker, and the pubkey sets are flagged stale while ids are
# in use. Switching either way renames the current sets into the other naming:
# the same edges, so a run sees what it saw before. Neo4j is read only when
# there are no complete sets to rename (a wiped Redis).
TRANSLATE_KEYS = 500
LOOKUP_BATCH = 10_000
IDS_DONE_MARKER_KEY = "migration:redis_backfill:ids:done"
PUBKEYS_STALE_KEY = "migration:redis_sets:pubkeys_stale"

_RELATIONSHIP_TO_PREFIX = {
    "FOLLOWS": FOLLOWED_BY_KEY_PREFIX,
    "MUTES": MUTED_BY_KEY_PREFIX,
    "REPORTS": REPORTED_BY_KEY_PREFIX,
}

# Written by brainstorm_profile_resolver, not this server; the GrapeRank worker reads it.
ZAPS_VIA_KEY_PREFIX = "zaps_via:"

# Forward sets: keyed by the source, holding its targets — the designations a user
# chose (kind-10040 keys and their zap signer). Neo4j holds the same edges, so a
# wiped Redis is rebuilt from them like the reverse sets above; nothing else would
# restore them (the resolver skips users it already settled).
_FORWARD_RELATIONSHIP_TO_PREFIX = {
    "DESIGNATES": DESIGNATES_KEY_PREFIX,
    "ZAPS_VIA": ZAPS_VIA_KEY_PREFIX,
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
    # The reverse sets only. Forward keys appear without this rebuild — the profile
    # resolver writes `zaps_via:` nightly, the 10040 consumer `designates:` — so after
    # a wipe they would make it skip, and the follow graph would never come back. A
    # rebuild cut short leaves reverse keys anyway: the reverse sets go first.
    for prefix in _RELATIONSHIP_TO_PREFIX.values():
        async for _ in redis_client.scan_iter(match=f"{prefix}*", count=1):
            return True
    return False


async def _has_pubkey_sets(redis_client) -> bool:
    for prefix in _RELATIONSHIP_TO_PREFIX.values():
        async for key in redis_client.scan_iter(match=f"{prefix}*", count=1000):
            if is_set_name(key[len(prefix) :], False):
                return True
    return False


async def _count_nostr_users() -> int:
    # Fast: neo4j keeps a label-count store, so this is effectively O(1).
    async with neo4j_driver.session() as neo4j_session:
        result = await neo4j_session.run("MATCH (n:NostrUser) RETURN count(n) AS total")
        record = await result.single()
        return int(record["total"]) if record else 0


async def _delete_sets(redis_client, ids: bool) -> int:
    """Remove every relationship set of one naming, leaving the other alone."""
    prefixes = [
        *_RELATIONSHIP_TO_PREFIX.values(),
        *_FORWARD_RELATIONSHIP_TO_PREFIX.values(),
    ]
    deleted = 0
    for prefix in prefixes:
        batch: list[str] = []
        async for key in redis_client.scan_iter(match=f"{prefix}*", count=5000):
            if is_set_name(key[len(prefix) :], ids):
                batch.append(key)
            if len(batch) >= 5000:
                deleted += await redis_client.unlink(*batch)
                batch = []
        if batch:
            deleted += await redis_client.unlink(*batch)
    return deleted


async def _other_names(names: set[str], to_ids: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    wanted = sorted(names)
    for start in range(0, len(wanted), LOOKUP_BATCH):
        chunk = wanted[start : start + LOOKUP_BATCH]
        if to_ids:
            found = await ids_for(chunk, create=False)
            out.update({pubkey: str(user_id) for pubkey, user_id in found.items()})
        else:
            users = await pubkeys_for([int(name) for name in chunk])
            out.update({str(user_id): pubkey for user_id, pubkey in users.items()})
    return out


async def _translate_sets(redis_client, to_ids: bool) -> tuple[int, int, int]:
    """Write every set of one naming again in the other: (sets, members, dropped).
    A key or member with no counterpart (a malformed pubkey has no id) is
    dropped and counted. The source sets are left as they are."""
    prefixes = [
        *_RELATIONSHIP_TO_PREFIX.values(),
        *_FORWARD_RELATIONSHIP_TO_PREFIX.values(),
    ]
    written = members_written = dropped = 0

    async def translate(prefix: str, keys: list[str]) -> None:
        nonlocal written, members_written, dropped
        pipe = redis_client.pipeline(transaction=False)
        for key in keys:
            pipe.smembers(key)
        sets = await pipe.execute()
        names = {key[len(prefix) :] for key in keys}
        for members in sets:
            names.update(m for m in members if is_set_name(m, not to_ids))
        other = await _other_names(names, to_ids)
        pipe = redis_client.pipeline(transaction=False)
        pending = 0
        for key, members in zip(keys, sets):
            target = other.get(key[len(prefix) :])
            renamed = [other[m] for m in members if m in other]
            if target is None:
                dropped += len(members)
                continue
            dropped += len(members) - len(renamed)
            if not renamed:
                continue
            written += 1
            members_written += len(renamed)
            for start in range(0, len(renamed), PIPELINE_FLUSH_EVERY):
                chunk = renamed[start : start + PIPELINE_FLUSH_EVERY]
                pipe.sadd(prefix + target, *chunk)
                pending += len(chunk)
                if pending >= PIPELINE_FLUSH_EVERY:
                    await pipe.execute()
                    pipe = redis_client.pipeline(transaction=False)
                    pending = 0
        if pending:
            await pipe.execute()

    for prefix in prefixes:
        batch: list[str] = []
        seen = 0
        async for key in redis_client.scan_iter(match=f"{prefix}*", count=5000):
            if not is_set_name(key[len(prefix) :], not to_ids):
                continue
            batch.append(key)
            if len(batch) >= TRANSLATE_KEYS:
                await translate(prefix, batch)
                seen += len(batch)
                batch = []
                if seen % 100_000 == 0:
                    logger.info(f"  {prefix} {seen} sets renamed.")
        if batch:
            await translate(prefix, batch)
    return written, members_written, dropped


async def _rebuild_sets(redis_client, ids: bool) -> None:
    total_users = await _count_nostr_users()
    for relationship, prefix in _RELATIONSHIP_TO_PREFIX.items():
        await _backfill_relationship(
            redis_client, relationship, prefix, total_users, ids=ids
        )
    for relationship, prefix in _FORWARD_RELATIONSHIP_TO_PREFIX.items():
        await _backfill_relationship(
            redis_client, relationship, prefix, total_users, forward=True, ids=ids
        )


async def _backfill_relationship(
    redis_client,
    relationship: str,
    prefix: str,
    total_users: int,
    forward: bool = False,
    ids: bool = False,
) -> int:
    # We iterate by source (publisher) rather than by target. Out-degree is bounded
    # by a user's contact-list / mute-list / report-list size (usually thousands at
    # most), whereas in-degree can run into the millions for popular pubkeys —
    # collecting incoming edges per target would force pathological list sizes.
    # Pages are a fresh, short-lived neo4j transaction over a contiguous slice of
    # NostrUser nodes ordered by pubkey (indexed via the unique constraint).
    # `collect` drops nulls: a user without an id is in no id set.
    name = "user_id" if ids else "pubkey"
    cypher = f"""
    MATCH (source:NostrUser)
    WHERE source.pubkey > $cursor
    WITH source ORDER BY source.pubkey LIMIT $page
    OPTIONAL MATCH (source)-[:{relationship}]->(target:NostrUser)
    RETURN source.pubkey AS cursor, source.{name} AS source,
           collect(target.{name}) AS targets
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
            result = await neo4j_session.run(cypher, cursor=cursor, page=PAGE_SIZE)
            async for record in result:
                source = record["source"]
                row_cursor = record["cursor"]
                if page_max_cursor is None or row_cursor > page_max_cursor:
                    page_max_cursor = row_cursor
                page_count += 1
                for target in [] if source is None else record["targets"]:
                    if forward:
                        pipe.sadd(f"{prefix}{source}", target)
                    else:
                        pipe.sadd(f"{prefix}{target}", source)
                    pending += 1
                    if pending >= PIPELINE_FLUSH_EVERY:
                        await pipe.execute()
                        pipe = redis_client.pipeline(transaction=False)
                        edges_written += pending
                        pending = 0

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
        if names_are_ids():
            await _build_id_sets_if_needed(redis_client)
            return
        if await redis_client.exists(PUBKEYS_STALE_KEY):
            await _rebuild_pubkey_sets_after_ids(redis_client)
            return

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
            f"from Neo4j, page size {PAGE_SIZE}."
        )
        await _rebuild_sets(redis_client, ids=False)
        await redis_client.set(DONE_MARKER_KEY, "1")
        logger.info(
            f"Redis relationship backfill complete; wrote marker '{DONE_MARKER_KEY}'."
        )
    finally:
        try:
            await redis_client.close()
        except Exception:
            pass


async def _build_id_sets_if_needed(redis_client) -> None:
    """First start with scores in Postgres: the id-named sets are made from the
    pubkey-named ones (from Neo4j when there are none). Runs before the ingest
    consumer starts, so nothing writes them meanwhile."""
    if await redis_client.exists(IDS_DONE_MARKER_KEY):
        logger.info(f"Redis id sets marker '{IDS_DONE_MARKER_KEY}' present — skipping.")
        return
    # Whatever pubkey sets exist are what runs read today, with or without the
    # done marker (older environments were filled before it existed).
    have_pubkey_sets = await _has_pubkey_sets(redis_client)
    if not have_pubkey_sets and not await _is_graph_db_populated():
        logger.info("Graph DB not populated yet — skipping Redis id sets.")
        return
    # From here on only the id sets are kept current.
    await redis_client.set(PUBKEYS_STALE_KEY, "1")
    removed = await _delete_sets(redis_client, ids=True)
    if have_pubkey_sets:
        logger.info(
            f"Renaming Redis sets to User ids ({removed} leftover keys removed)."
        )
        sets, members, dropped = await _translate_sets(redis_client, to_ids=True)
        logger.info(f"{sets} sets, {members} members renamed; {dropped} without an id.")
    else:
        logger.info(
            f"Building Redis id sets from Neo4j ({removed} leftover keys removed)."
        )
        await _rebuild_sets(redis_client, ids=True)
    await redis_client.set(IDS_DONE_MARKER_KEY, "1")
    logger.info(f"Redis id sets complete; wrote marker '{IDS_DONE_MARKER_KEY}'.")


async def _rebuild_pubkey_sets_after_ids(redis_client) -> None:
    """Back from ids to pubkeys: the pubkey sets missed everything ingested
    meanwhile, so they are replaced by the id sets renamed back (from Neo4j when
    the id sets were never finished). The id sets are stale from now."""
    have_id_sets = bool(await redis_client.exists(IDS_DONE_MARKER_KEY))
    removed = await _delete_sets(redis_client, ids=False)
    if have_id_sets:
        logger.info(
            f"Renaming Redis sets back to pubkeys ({removed} stale keys removed)."
        )
        sets, members, dropped = await _translate_sets(redis_client, to_ids=False)
        logger.info(f"{sets} sets, {members} members renamed; {dropped} unknown ids.")
    else:
        logger.info(
            f"Rebuilding Redis pubkey sets from Neo4j ({removed} stale keys removed)."
        )
        await _rebuild_sets(redis_client, ids=False)
    await redis_client.set(DONE_MARKER_KEY, "1")
    await redis_client.delete(IDS_DONE_MARKER_KEY)
    await redis_client.delete(PUBKEYS_STALE_KEY)
    logger.info("Redis pubkey sets rebuilt.")
