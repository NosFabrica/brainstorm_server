from neo4j import AsyncDriver as AsyncNeoDriver

from app.core.tier_thresholds import (
    DEFAULT_VERIFIED_THRESHOLD,
    TIER_HIGH,
    TIER_MEDIUM,
    TIER_MEDIUM_HIGH,
)
from app.schemas.schemas import (
    ConnectionStats,
    ConnectionTierCounts,
    UserConnection,
    UserConnectionItem,
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


# Which of the given pubkeys follow at least one user (i.e. are schedulable).
async def pubkeys_following_someone(
    session: AsyncNeoDriver, pubkeys: list[str]
) -> set[str]:
    if not pubkeys:
        return set()
    query = """
    MATCH (user:NostrUser)-[:FOLLOWS]->(:NostrUser)
    WHERE user.pubkey IN $pubkeys
    RETURN DISTINCT user.pubkey AS pubkey
    """
    result = await session.run(query, pubkeys=pubkeys)
    return {record["pubkey"] async for record in result}


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
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
    trusted_reporters_key: str,
    verified_threshold: float,
) -> tuple[float | None, int, int, int, bool, int]:
    """One round-trip: user's own influence + outbound counts +
    flagged_by_observer + DISTINCT flagged_count across all relationships."""
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})
    CALL (user) { MATCH (user)-[:FOLLOWS]->(o:NostrUser)  RETURN count(o) AS following }
    CALL (user) { MATCH (user)-[:MUTES]->(o:NostrUser)    RETURN count(o) AS muting }
    CALL (user) { MATCH (user)-[:REPORTS]->(o:NostrUser)  RETURN count(o) AS reporting }
    CALL (user) {
        MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)
        WHERE other[$influence_key] IS NOT NULL
          AND other[$influence_key] < $verified_threshold
          AND coalesce(other[$trusted_reporters_key], 0) >= 2
        RETURN count(DISTINCT other) AS flagged_count
    }
    RETURN
        user[$influence_key] AS influence,
        following,
        muting,
        reporting,
        flagged_count,
        (
            user[$influence_key] IS NOT NULL
            AND user[$influence_key] < $verified_threshold
            AND coalesce(user[$trusted_reporters_key], 0) >= 2
        ) AS flagged_by_observer
    """
    result = await session.run(
        query,
        pubkey=pubkey,
        influence_key=influence_key,
        trusted_reporters_key=trusted_reporters_key,
        verified_threshold=verified_threshold,
    )
    record = await result.single()
    if not record:
        return None, 0, 0, 0, False, 0
    return (
        record["influence"],
        int(record["following"] or 0),
        int(record["muting"] or 0),
        int(record["reporting"] or 0),
        bool(record["flagged_by_observer"]),
        int(record["flagged_count"] or 0),
    )


_TIER_PREDICATES: dict[str, str] = {
    # Bucket names match the GR result writer's count_values keys (see
    # message_queue_consumer.py). Placeholders:
    #   __INF__ → other[$influence_key]
    #   __TR__  → coalesce(other[$trusted_reporters_key], 0)
    # Parameter names $tier_high/$tier_medium_high/$tier_medium are the upper
    # bounds of high/medium_high/medium respectively — kept stable as API
    # surface, the semantic boundary value doesn't change with renaming.
    "high": "__INF__ IS NOT NULL AND __INF__ >= $tier_high",
    "medium_high": "__INF__ IS NOT NULL AND __INF__ >= $tier_medium_high AND __INF__ < $tier_high",
    "medium": "__INF__ IS NOT NULL AND __INF__ >= $tier_medium AND __INF__ < $tier_medium_high",
    "medium_low": "__INF__ IS NOT NULL AND __INF__ >= $vt AND __INF__ < $tier_medium",
    "low": "(__INF__ IS NULL OR (__INF__ < $vt AND __TR__ < 2))",
    "low_and_reported_by_2_or_more_trusted_pubkeys": "__INF__ IS NOT NULL AND __INF__ < $vt AND __TR__ >= 2",
}


def _build_tier_predicate(tier: str | None) -> str:
    if not tier or tier not in _TIER_PREDICATES:
        return ""
    return (
        "("
        + _TIER_PREDICATES[tier]
        .replace("__INF__", "other[$influence_key]")
        .replace("__TR__", "coalesce(other[$trusted_reporters_key], 0)")
        + ")"
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
    order: str = "desc",
    tier: str | None = None,
    min_influence: float | None = None,
    verified_threshold: float | None = None,
    tier_high: float | None = None,
    tier_medium_high: float | None = None,
    tier_medium: float | None = None,
    with_total: bool = False,
) -> tuple[list[UserConnectionItem], tuple[float, str] | None, int | None]:
    """Cursor-paginated connection list ordered by (influence <order>, pubkey ASC),
    optionally filtered to a single tier and/or `min_influence`.

    `order` is "desc" (highest influence first) or "asc" (lowest first).
    When `with_total` is set, a second `CALL` subquery counts all matches for the
    same filter (cursor-independent) in the SAME round-trip.
    Returns (items, last_record_cursor_or_none, total_or_none).
    """
    scoped_pattern = _scoped_match_pattern(rel_type, direction)
    inf_order = "ASC" if order == "asc" else "DESC"
    # Cursor predicate must match the sort direction. Secondary sort stays
    # ASC by pubkey in both cases — cursor compares pubkey with `>` so we
    # always step "later" in tied groups.
    inf_cmp = ">" if order == "asc" else "<"

    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "trusted_reporters_key": trusted_reporters_key,
        "limit": limit,
    }
    # Filter predicates (tier / min_influence) are cursor-independent and shared
    # by the page subquery and the optional count subquery. The cursor predicate
    # applies to the page only.
    filter_parts: list[str] = []
    tier_pred = _build_tier_predicate(tier)
    if tier_pred:
        # Bind every threshold the predicate references, defaulting to the
        # canonical constants so a `tier` passed without explicit bounds can't
        # leave a Cypher parameter unbound.
        params["vt"] = (
            verified_threshold
            if verified_threshold is not None
            else DEFAULT_VERIFIED_THRESHOLD
        )
        params["tier_high"] = tier_high if tier_high is not None else TIER_HIGH
        params["tier_medium_high"] = (
            tier_medium_high if tier_medium_high is not None else TIER_MEDIUM_HIGH
        )
        params["tier_medium"] = tier_medium if tier_medium is not None else TIER_MEDIUM
        filter_parts.append(tier_pred)
    if min_influence is not None:
        params["min_influence"] = min_influence
        filter_parts.append(
            "(other[$influence_key] IS NOT NULL AND other[$influence_key] >= $min_influence)"
        )

    page_parts = list(filter_parts)
    if cursor_inf is not None and cursor_pk is not None:
        params["cursor_inf"] = cursor_inf
        params["cursor_pk"] = cursor_pk
        page_parts.append(
            f"(sort_inf {inf_cmp} $cursor_inf "
            "OR (sort_inf = $cursor_inf AND other.pubkey > $cursor_pk))"
        )
    page_where = ("WHERE " + " AND ".join(page_parts)) if page_parts else ""
    count_where = ("WHERE " + " AND ".join(filter_parts)) if filter_parts else ""

    # Both subqueries end in an aggregation (collect / count), so each always
    # yields exactly one row — even on an empty/last page. A plain row-streaming
    # CALL returning zero rows would drop the outer row and lose `total`.
    count_block = (
        f"""
    CALL (user) {{
        MATCH {scoped_pattern}
        {count_where}
        RETURN count(other) AS total
    }}"""
        if with_total
        else ""
    )
    return_tail = "rows, total" if with_total else "rows"

    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})
    CALL (user) {{
        MATCH {scoped_pattern}
        WITH other, coalesce(other[$influence_key], -1.0) AS sort_inf
        {page_where}
        WITH other, sort_inf
        ORDER BY sort_inf {inf_order}, other.pubkey ASC
        LIMIT $limit
        RETURN collect({{
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key],
            sort_inf: sort_inf
        }}) AS rows
    }}{count_block}
    RETURN {return_tail}
    """

    result = await session.run(query, **params)
    record = await result.single()
    rows = record["rows"] if record else []
    total = (
        (int(record["total"]) if record and record["total"] is not None else 0)
        if with_total
        else None
    )

    items = [
        UserConnectionItem(
            pubkey=row["pubkey"],
            influence=row["influence"],
            trusted_reporters=(
                int(row["trusted_reporters"])
                if row["trusted_reporters"] is not None
                else None
            ),
        )
        for row in rows
    ]

    last_cursor: tuple[float, str] | None = None
    if len(rows) == limit and rows:
        last = rows[-1]
        last_cursor = (float(last["sort_inf"]), str(last["pubkey"]))

    return items, last_cursor, total


async def get_paginated_flagged_connections(
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
    trusted_reporters_key: str,
    verified_threshold: float,
    limit: int,
    cursor_inf: float | None,
    cursor_pk: str | None,
    order: str = "desc",
    with_total: bool = False,
) -> tuple[list[UserConnectionItem], tuple[float, str] | None, int | None]:
    """DISTINCT flagged users across any relationship to `pubkey`. A user is
    flagged when (from `pubkey`'s perspective) their influence is below
    `verified_threshold` AND at least 2 trusted reporters have reported them.
    Cursor-paginated by (influence <order>, pubkey ASC). Same shape as
    get_paginated_section_connections so the client can reuse one item type.
    When `with_total` is set, the DISTINCT flagged count is computed in the same
    round-trip. Returns (items, last_record_cursor_or_none, total_or_none)."""
    inf_order = "ASC" if order == "asc" else "DESC"
    inf_cmp = ">" if order == "asc" else "<"

    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "trusted_reporters_key": trusted_reporters_key,
        "vt": verified_threshold,
        "limit": limit,
    }
    cursor_clause = ""
    if cursor_inf is not None and cursor_pk is not None:
        params["cursor_inf"] = cursor_inf
        params["cursor_pk"] = cursor_pk
        cursor_clause = (
            f"AND (sort_inf {inf_cmp} $cursor_inf "
            "OR (sort_inf = $cursor_inf AND other.pubkey > $cursor_pk))"
        )

    # Both subqueries end in an aggregation (collect / count) so each always
    # yields exactly one row, even on an empty/last page.
    count_block = (
        """
    CALL (user) {
        MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)
        WHERE other[$influence_key] IS NOT NULL
          AND other[$influence_key] < $vt
          AND coalesce(other[$trusted_reporters_key], 0) >= 2
        RETURN count(DISTINCT other) AS total
    }"""
        if with_total
        else ""
    )
    return_tail = "rows, total" if with_total else "rows"

    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})
    CALL (user) {{
        MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)
        WITH DISTINCT other,
             coalesce(other[$influence_key], -1.0) AS sort_inf
        WHERE other[$influence_key] IS NOT NULL
          AND other[$influence_key] < $vt
          AND coalesce(other[$trusted_reporters_key], 0) >= 2
          {cursor_clause}
        WITH other, sort_inf
        ORDER BY sort_inf {inf_order}, other.pubkey ASC
        LIMIT $limit
        RETURN collect({{
            pubkey: other.pubkey,
            influence: other[$influence_key],
            trusted_reporters: other[$trusted_reporters_key],
            sort_inf: sort_inf
        }}) AS rows
    }}{count_block}
    RETURN {return_tail}
    """

    result = await session.run(query, **params)
    record = await result.single()
    rows = record["rows"] if record else []
    total = (
        (int(record["total"]) if record and record["total"] is not None else 0)
        if with_total
        else None
    )
    items = [
        UserConnectionItem(
            pubkey=row["pubkey"],
            influence=row["influence"],
            trusted_reporters=(
                int(row["trusted_reporters"])
                if row["trusted_reporters"] is not None
                else None
            ),
        )
        for row in rows
    ]
    last_cursor: tuple[float, str] | None = None
    if len(rows) == limit and rows:
        last = rows[-1]
        last_cursor = (float(last["sort_inf"]), str(last["pubkey"]))
    return items, last_cursor, total


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
    trusted_reporters_key: str,
    verified_threshold: float,
    tier_high: float,
    tier_medium_high: float,
    tier_medium: float,
) -> dict[str, ConnectionStats]:
    """Single-query version of get_section_stats covering all 6 relationships.
    ~20% faster than 6 parallel sessions on heavy accounts (1 round-trip)."""
    blocks: list[str] = ["MATCH (user:NostrUser {pubkey: $pubkey})"]
    return_fields: list[str] = []
    for name, rel_type, direction in _STATS_KINDS:
        pattern = _scoped_match_pattern(rel_type, direction)
        # `flagged` is its own bucket (influence < vt AND trusted_reporters >= 2);
        # `unverified` excludes flagged so the buckets sum to `total`.
        blocks.append(
            f"""
        CALL (user) {{
            MATCH {pattern}
            RETURN
              count(*) AS {name}_total,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $vt THEN 1 END) AS {name}_verified,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_high THEN 1 END) AS {name}_th,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_medium_high AND other[$influence_key] < $tier_high THEN 1 END) AS {name}_tt,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $tier_medium AND other[$influence_key] < $tier_medium_high THEN 1 END) AS {name}_tn,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] >= $vt AND other[$influence_key] < $tier_medium THEN 1 END) AS {name}_tl,
              count(CASE WHEN other[$influence_key] IS NOT NULL AND other[$influence_key] < $vt AND coalesce(other[$trusted_reporters_key], 0) >= 2 THEN 1 END) AS {name}_tf,
              count(CASE WHEN other[$influence_key] IS NULL OR (other[$influence_key] < $vt AND coalesce(other[$trusted_reporters_key], 0) < 2) THEN 1 END) AS {name}_tu
        }}"""
        )
        return_fields.extend(
            f"{name}_{suffix}"
            for suffix in ("total", "verified", "th", "tt", "tn", "tl", "tf", "tu")
        )
    blocks.append("RETURN " + ", ".join(return_fields))
    query = "\n".join(blocks)

    result = await session.run(
        query,
        pubkey=pubkey,
        influence_key=influence_key,
        trusted_reporters_key=trusted_reporters_key,
        vt=verified_threshold,
        tier_high=tier_high,
        tier_medium_high=tier_medium_high,
        tier_medium=tier_medium,
    )
    record = await result.single()

    def _empty() -> ConnectionStats:
        return ConnectionStats(
            total=0,
            verified=0,
            tier_counts=ConnectionTierCounts(
                high=0,
                medium_high=0,
                medium=0,
                medium_low=0,
                low=0,
                low_and_reported_by_2_or_more_trusted_pubkeys=0,
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
                medium_high=int(record[f"{name}_tt"] or 0),
                medium=int(record[f"{name}_tn"] or 0),
                medium_low=int(record[f"{name}_tl"] or 0),
                low=int(record[f"{name}_tu"] or 0),
                low_and_reported_by_2_or_more_trusted_pubkeys=int(record[f"{name}_tf"] or 0),
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


# ============================================================================
# Open Ranking (ORE) helpers
# ============================================================================


async def batch_influence_for_pubkeys(
    session: AsyncNeoDriver,
    pubkeys: list[str],
    observer_pubkey: str,
) -> dict[str, float | None]:
    """Return {pubkey -> influence_<observer_pubkey>} for every pubkey in
    `pubkeys`. Unknown pubkeys and pubkeys with no influence property for the
    given observer are mapped to None. Used by ORE-03 (POST /rank/pubkeys).
    """
    if not pubkeys:
        return {}

    influence_key = f"influence_{observer_pubkey}"
    query = """
    UNWIND $pubkeys AS pk
    OPTIONAL MATCH (user:NostrUser {pubkey: pk})
    RETURN pk AS pubkey, user[$influence_key] AS influence
    """
    result = await session.run(
        query, pubkeys=pubkeys, influence_key=influence_key
    )
    out: dict[str, float | None] = {pk: None for pk in pubkeys}
    async for record in result:
        out[record["pubkey"]] = record["influence"]
    return out


async def get_top_inbound_by_influence(
    session: AsyncNeoDriver,
    pubkey: str,
    observer_pubkey: str,
    relation: str,
    limit: int,
) -> list[UserConnection]:
    """Top `limit` users with an incoming `relation` edge into `pubkey`,
    sorted by `influence_<observer_pubkey>` DESC, pubkey ASC. Users with no
    influence property for the observer get sort value -1 (last).

    Used by ORE-06 (relation=FOLLOWS) and ORE-07 (relation=MUTES).
    """
    if relation not in ("FOLLOWS", "MUTES", "REPORTS"):
        raise ValueError(f"Unsupported relation: {relation}")

    influence_key = f"influence_{observer_pubkey}"
    query = f"""
    MATCH (other:NostrUser)-[:{relation}]->(user:NostrUser {{pubkey: $pubkey}})
    WITH other, coalesce(other[$influence_key], -1.0) AS sort_inf
    RETURN other.pubkey AS pubkey, other[$influence_key] AS influence
    ORDER BY sort_inf DESC, other.pubkey ASC
    LIMIT $limit
    """
    result = await session.run(
        query, pubkey=pubkey, influence_key=influence_key, limit=int(limit)
    )
    return [
        UserConnection(pubkey=record["pubkey"], influence=record["influence"])
        async for record in result
    ]


# Shortest paths


async def get_all_shortest_follow_paths(
    session: AsyncNeoDriver,
    from_pubkey: str,
    to_pubkey: str,
    max_hops: int,
    max_paths: int,
) -> tuple[list[list[str]], int]:
    """All shortest directed FOLLOWS paths from `from_pubkey` to `to_pubkey`.

    Returns (paths, true_path_count): up to `max_paths` materialized pubkey
    chains (each inclusive of both endpoints) plus the TRUE number of shortest
    paths found, so the caller can surface an exact capped/uncapped flag.
    ([], 0) when either pubkey is absent from the graph or no directed path
    exists within `max_hops`.

    Cypher forbids parameters in variable-length bounds (`*..$maxHops` is
    illegal), so `max_hops` is interpolated — guarded here, at the
    interpolation site, independently of any caller-side validation. Both
    pubkeys and `max_paths` are real query parameters. Callers must NOT pass
    from_pubkey == to_pubkey: Neo4j rejects same-node shortest paths (the
    service short-circuits that case; see ADR 0001).
    """
    if type(max_hops) is not int or not 1 <= max_hops <= 50:
        raise ValueError(f"max_hops must be an int in [1, 50], got {max_hops!r}")

    query = f"""
    MATCH (a:NostrUser {{pubkey: $from_pubkey}}), (b:NostrUser {{pubkey: $to_pubkey}})
    MATCH p = allShortestPaths((a)-[:FOLLOWS*..{max_hops}]->(b))
    WITH [n IN nodes(p) | n.pubkey] AS chain
    RETURN collect(chain)[..$max_paths] AS paths, count(*) AS path_count
    """
    result = await session.run(
        query,
        from_pubkey=from_pubkey,
        to_pubkey=to_pubkey,
        max_paths=int(max_paths),
    )
    record = await result.single()
    if record is None:
        return [], 0
    return record["paths"], record["path_count"]
