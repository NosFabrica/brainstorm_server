from neo4j import AsyncDriver as AsyncNeoDriver

from app.schemas.schemas import (
    ConnectionStats,
    ConnectionTierCounts,
    UserConnection,
    UserGraphData,
)


# ----------------- Helper Function -----------------


async def _get_pubkeys_with_influence(
    session: AsyncNeoDriver,
    pubkey: str,
    observer: str | None,
    relation: str,
    direction: str = "incoming",
) -> list[UserConnection]:
    if direction == "incoming":
        match_clause = (
            f"(other:NostrUser)-[:{relation}]->(user:NostrUser {{pubkey: $pubkey}})"
        )
    else:
        match_clause = (
            f"(user:NostrUser {{pubkey: $pubkey}})-[:{relation}]->(other:NostrUser)"
        )

    query = f"""
    MATCH {match_clause}
    RETURN 
        other.pubkey AS pubkey,
        other[$influence_key] AS influence
    """

    influence_key = f"influence_{observer}" if observer else f"influence_{pubkey}"
    result = await session.run(query, pubkey=pubkey, influence_key=influence_key)

    return [
        UserConnection(pubkey=record["pubkey"], influence=record["influence"])
        async for record in result
    ]


# ----------------- Refactored Functions -----------------


# Follows
async def get_list_of_pubkeys_following_user(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="FOLLOWS", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_follows(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="FOLLOWS", direction="outgoing"
    )


# Mutes
async def get_list_of_pubkeys_muting_user(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="MUTES", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_mutes(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="MUTES", direction="outgoing"
    )


# Reports
async def get_list_of_pubkeys_reporting_user(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="REPORTS", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_reports(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="REPORTS", direction="outgoing"
    )


# ----------------- Follows -----------------


# Number of users following a given pubkey
async def count_following_user(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:FOLLOWS]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey follows
async def count_user_follows(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:FOLLOWS]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# ----------------- Mutes -----------------


# Number of users muting a given pubkey
async def count_muting_user(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:MUTES]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey mutes
async def count_user_mutes(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:MUTES]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# ----------------- Reports -----------------


# Number of users reporting a given pubkey
async def count_reporting_user(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:REPORTS]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey reports
async def count_user_reports(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:REPORTS]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


async def get_influence_for_observer(
    session: AsyncNeoDriver, pubkey: str, observer_pubkey: str
) -> float | None:
    """
    Returns the value of 'influence_<observer_pubkey>' for the NostrUser with pubkey.
    Returns None if the user or property does not exist.
    """
    property_name = f"influence_{observer_pubkey}"
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})
    RETURN user[$property_name] AS influence
    """

    result = await session.run(query, pubkey=pubkey, property_name=property_name)
    record = await result.single()
    return record["influence"] if record and record["influence"] is not None else None


# ----------------- overview / stats / paginated connections -----------------
#
# Notes on the Cypher patterns below:
# - Match pattern direction "in" = (other)-[:REL]->(user); "out" = (user)-[:REL]->(other).
# - Neo4j quirk: `WITH other[$dynamic_key] AS inf` aliases the value as Infinity
#   for every row. Inline `other[$influence_key]` in CASE WHEN / RETURN to get
#   real values. Wrapping in `coalesce(other[$key], default)` also works.


def _scoped_match_pattern(rel_type: str, direction: str) -> str:
    """MATCH pattern assuming `user` is already bound in the surrounding scope
    (e.g. inside a `CALL (user)` subquery)."""
    if direction == "in":
        return f"(other:NostrUser)-[:{rel_type}]->(user)"
    return f"(user)-[:{rel_type}]->(other:NostrUser)"


async def get_outbound_counts_and_influence(
    session: AsyncNeoDriver, pubkey: str, influence_key: str
) -> tuple[float | None, int, int, int]:
    """One round-trip: user's own influence + counts of following/muting/reporting."""
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})
    CALL (user) { MATCH (user)-[:FOLLOWS]->(o:NostrUser)  RETURN count(o) AS following }
    CALL (user) { MATCH (user)-[:MUTES]->(o:NostrUser)    RETURN count(o) AS muting }
    CALL (user) { MATCH (user)-[:REPORTS]->(o:NostrUser)  RETURN count(o) AS reporting }
    RETURN user[$influence_key] AS influence, following, muting, reporting
    """
    result = await session.run(query, pubkey=pubkey, influence_key=influence_key)
    record = await result.single()
    if not record:
        return None, 0, 0, 0
    return (
        record["influence"],
        int(record["following"] or 0),
        int(record["muting"] or 0),
        int(record["reporting"] or 0),
    )


async def get_paginated_section_connections(
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
    trusted_reporters_key: str,
    rel_type: str,
    direction: str,
    limit: int,
    cursor_inf: float | None,
    cursor_pk: str | None,
    compute_stats: bool,
    verified_threshold: float,
    tier_high: float,
    tier_trusted: float,
    tier_neutral: float,
) -> tuple[list[UserConnection], ConnectionStats | None, tuple[float, str] | None]:
    """Cursor-paginated connection list ordered by (influence DESC, pubkey ASC).
    On first page (no cursor), also returns ConnectionStats in a single query.

    Returns (items, stats_or_none, last_record_cursor_or_none).
    """
    scoped_pattern = _scoped_match_pattern(rel_type, direction)

    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "trusted_reporters_key": trusted_reporters_key,
        "limit": limit,
        "verified_threshold": verified_threshold,
        "tier_high": tier_high,
        "tier_trusted": tier_trusted,
        "tier_neutral": tier_neutral,
    }
    cursor_clause = ""
    if cursor_inf is not None and cursor_pk is not None:
        params["cursor_inf"] = cursor_inf
        params["cursor_pk"] = cursor_pk
        cursor_clause = (
            "WHERE sort_inf < $cursor_inf "
            "OR (sort_inf = $cursor_inf AND other.pubkey > $cursor_pk)"
        )

    stats_subquery = (
        f"""
        CALL (user) {{
            MATCH {scoped_pattern}
            RETURN
                count(*) AS total,
                count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $verified_threshold THEN 1 END) AS verified,
                count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_high THEN 1 END) AS tier_high,
                count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_trusted AND other[$influence_key] < $tier_high THEN 1 END) AS tier_trusted,
                count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_neutral AND other[$influence_key] < $tier_trusted THEN 1 END) AS tier_neutral,
                count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $verified_threshold AND other[$influence_key] < $tier_neutral THEN 1 END) AS tier_low,
                count(CASE WHEN other[$influence_key] IS NULL OR other[$influence_key] < $verified_threshold THEN 1 END) AS tier_unverified
        }}
        """
        if compute_stats
        else ""
    )
    stats_return = (
        ", total, verified, tier_high, tier_trusted, tier_neutral, tier_low, tier_unverified"
        if compute_stats
        else ""
    )

    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})
    {stats_subquery}
    CALL (user) {{
        MATCH {scoped_pattern}
        WITH other, coalesce(other[$influence_key], -1.0) AS sort_inf
        {cursor_clause}
        RETURN other.pubkey AS pubkey,
               other[$influence_key] AS influence,
               other[$trusted_reporters_key] AS trusted_reporters,
               sort_inf
        ORDER BY sort_inf DESC, other.pubkey ASC
        LIMIT $limit
    }}
    RETURN pubkey, influence, trusted_reporters, sort_inf{stats_return}
    """

    result = await session.run(query, **params)
    records = [rec async for rec in result]

    items = [
        UserConnection(
            pubkey=rec["pubkey"],
            influence=rec["influence"],
            trusted_reporters=rec["trusted_reporters"],
        )
        for rec in records
    ]

    last_cursor: tuple[float, str] | None = None
    if len(records) == limit and records:
        last = records[-1]
        last_cursor = (float(last["sort_inf"]), str(last["pubkey"]))

    stats: ConnectionStats | None = None
    if compute_stats:
        if records:
            first = records[0]
            stats = ConnectionStats(
                total=int(first["total"] or 0),
                verified=int(first["verified"] or 0),
                tier_counts=ConnectionTierCounts(
                    high=int(first["tier_high"] or 0),
                    trusted=int(first["tier_trusted"] or 0),
                    neutral=int(first["tier_neutral"] or 0),
                    low=int(first["tier_low"] or 0),
                    unverified=int(first["tier_unverified"] or 0),
                ),
            )
        else:
            stats = ConnectionStats(
                total=0,
                verified=0,
                tier_counts=ConnectionTierCounts(
                    high=0, trusted=0, neutral=0, low=0, unverified=0
                ),
            )

    return items, stats, last_cursor


_STATS_KINDS: list[tuple[str, str, str]] = [
    ("followed_by", "FOLLOWS", "in"),
    ("following", "FOLLOWS", "out"),
    ("muted_by", "MUTES", "in"),
    ("muting", "MUTES", "out"),
    ("reported_by", "REPORTS", "in"),
    ("reporting", "REPORTS", "out"),
]


async def get_all_section_stats(
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
    verified_threshold: float,
    tier_high: float,
    tier_trusted: float,
    tier_neutral: float,
) -> dict[str, ConnectionStats]:
    """Single-query version of get_section_stats covering all 6 relationships.
    ~20% faster than 6 parallel sessions on heavy accounts (1 round-trip)."""
    blocks: list[str] = ["MATCH (user:NostrUser {pubkey: $pubkey})"]
    return_fields: list[str] = []
    for name, rel_type, direction in _STATS_KINDS:
        pattern = _scoped_match_pattern(rel_type, direction)
        blocks.append(
            f"""
        CALL (user) {{
            MATCH {pattern}
            RETURN
              count(*) AS {name}_total,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $vt THEN 1 END) AS {name}_verified,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_high THEN 1 END) AS {name}_th,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_trusted AND other[$influence_key] < $tier_high THEN 1 END) AS {name}_tt,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_neutral AND other[$influence_key] < $tier_trusted THEN 1 END) AS {name}_tn,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $vt AND other[$influence_key] < $tier_neutral THEN 1 END) AS {name}_tl,
              count(CASE WHEN other[$influence_key] IS NULL OR other[$influence_key] < $vt THEN 1 END) AS {name}_tu
        }}"""
        )
        return_fields.extend(
            f"{name}_{suffix}"
            for suffix in ("total", "verified", "th", "tt", "tn", "tl", "tu")
        )
    blocks.append("RETURN " + ", ".join(return_fields))
    query = "\n".join(blocks)

    result = await session.run(
        query,
        pubkey=pubkey,
        influence_key=influence_key,
        vt=verified_threshold,
        tier_high=tier_high,
        tier_trusted=tier_trusted,
        tier_neutral=tier_neutral,
    )
    record = await result.single()

    def _empty() -> ConnectionStats:
        return ConnectionStats(
            total=0,
            verified=0,
            tier_counts=ConnectionTierCounts(
                high=0, trusted=0, neutral=0, low=0, unverified=0
            ),
        )

    if not record:
        return {name: _empty() for name, _, _ in _STATS_KINDS}

    return {
        name: ConnectionStats(
            total=int(record[f"{name}_total"] or 0),
            verified=int(record[f"{name}_verified"] or 0),
            tier_counts=ConnectionTierCounts(
                high=int(record[f"{name}_th"] or 0),
                trusted=int(record[f"{name}_tt"] or 0),
                neutral=int(record[f"{name}_tn"] or 0),
                low=int(record[f"{name}_tl"] or 0),
                unverified=int(record[f"{name}_tu"] or 0),
            ),
        )
        for name, _, _ in _STATS_KINDS
    }


async def get_user_graph_data(
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
    trusted_reporters_key: str,
) -> UserGraphData:
    """Single Cypher returning all 6 relationship lists (full, unpaginated) plus
    the user's own influence — used by /self and /user/{pubkey}."""
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})

    CALL (user) {
        MATCH (other:NostrUser)-[:FOLLOWS]->(user)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS followed_by
    }

    CALL (user) {
        MATCH (user)-[:FOLLOWS]->(other:NostrUser)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS following
    }

    CALL (user) {
        MATCH (other:NostrUser)-[:MUTES]->(user)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS muted_by
    }

    CALL (user) {
        MATCH (user)-[:MUTES]->(other:NostrUser)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS muting
    }

    CALL (user) {
        MATCH (other:NostrUser)-[:REPORTS]->(user)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS reported_by
    }

    CALL (user) {
        MATCH (user)-[:REPORTS]->(other:NostrUser)
        RETURN collect({
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key]
        }) AS reporting
    }

    RETURN
        user[$influence_key] AS influence,
        followed_by,
        following,
        muted_by,
        muting,
        reported_by,
        reporting
    """
    result = await session.run(
        query,
        pubkey=pubkey,
        influence_key=influence_key,
        trusted_reporters_key=trusted_reporters_key,
    )
    record = await result.single()
    if not record:
        return UserGraphData(
            influence=None,
            followed_by=[],
            following=[],
            muted_by=[],
            muting=[],
            reported_by=[],
            reporting=[],
        )
    return UserGraphData(
        influence=record["influence"],
        followed_by=[UserConnection(**x) for x in record["followed_by"]],
        following=[UserConnection(**x) for x in record["following"]],
        muted_by=[UserConnection(**x) for x in record["muted_by"]],
        muting=[UserConnection(**x) for x in record["muting"]],
        reported_by=[UserConnection(**x) for x in record["reported_by"]],
        reporting=[UserConnection(**x) for x in record["reporting"]],
    )
