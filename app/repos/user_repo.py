from typing import NamedTuple

from neo4j import AsyncDriver as AsyncNeoDriver

from app.core.tier_thresholds import (
    FLAGGED_TIER,
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


class OutboundCounts(NamedTuple):
    """A subject's own influence plus its three outbound counts."""

    influence: float | None
    following: int
    muting: int
    reporting: int


class OutboundOverview(NamedTuple):
    """What `/overview` needs about one subject, from one query — the counts
    plus everything the observer's verified line decides."""

    influence: float | None
    following: int
    muting: int
    reporting: int
    flagged_by_observer: bool
    flagged_count: int
    tier: str | None


# Shared by the lean and the full query so they can't drift. Single braces: both
# callers interpolate this into an f-string that has already doubled its own.
_OUTBOUND_COUNT_BLOCKS = """
    CALL (user) { MATCH (user)-[:FOLLOWS]->(o:NostrUser)  RETURN count(o) AS following }
    CALL (user) { MATCH (user)-[:MUTES]->(o:NostrUser)    RETURN count(o) AS muting }
    CALL (user) { MATCH (user)-[:REPORTS]->(o:NostrUser)  RETURN count(o) AS reporting }"""


async def get_counts_and_influence(
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
) -> OutboundCounts:
    """Influence + outbound counts, and nothing the verified line touches.

    The lean half of `get_outbound_counts_and_influence`, for callers surfacing
    neither the flagged fields nor verified/tier — currently ORE-02. Skips the
    flagged DISTINCT scan over every inbound edge, and takes no line, so the
    caller needs no preset read. Need a line-driven field? Use the full query.
    """
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}}){_OUTBOUND_COUNT_BLOCKS}
    RETURN user[$influence_key] AS influence, following, muting, reporting
    """
    result = await session.run(query, pubkey=pubkey, influence_key=influence_key)
    record = await result.single()
    if not record:
        return OutboundCounts(None, 0, 0, 0)
    return OutboundCounts(
        influence=record["influence"],
        following=int(record["following"] or 0),
        muting=int(record["muting"] or 0),
        reporting=int(record["reporting"] or 0),
    )


async def get_outbound_counts_and_influence(
    session: AsyncNeoDriver,
    pubkey: str,
    influence_key: str,
    trusted_reporters_key: str,
    verified_line: float,
) -> OutboundOverview:
    """One round-trip: the user's own influence and tier, plus outbound counts,
    flagged_by_observer and a DISTINCT flagged_count across all relationships.

    The user's own tier uses the same predicates the section rows do, against
    the follower cutoff (`verified_line`) — the general trusted bar, since the
    subject isn't rating anyone here.

    "Flagged" is *not verified* (influence `<= verified_line`, the complement of
    the strict `>` used everywhere else) AND reported by 2+ trusted accounts."""
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}}){_OUTBOUND_COUNT_BLOCKS}
    CALL (user) {{
        MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)
        WHERE {_expand(_TIER_PREDICATES[FLAGGED_TIER])}
        RETURN count(DISTINCT other) AS flagged_count
    }}
    RETURN
        user[$influence_key] AS influence,
        following,
        muting,
        reporting,
        flagged_count,
        {_tier_case("user")} AS tier,
        {_expand(_TIER_PREDICATES[FLAGGED_TIER], "user")} AS flagged_by_observer
    """
    result = await session.run(
        query,
        pubkey=pubkey,
        influence_key=influence_key,
        trusted_reporters_key=trusted_reporters_key,
        verified_line=verified_line,
        **_tier_band_params(),
    )
    record = await result.single()
    if not record:
        return OutboundOverview(None, 0, 0, 0, False, 0, None)
    return OutboundOverview(
        influence=record["influence"],
        following=int(record["following"] or 0),
        muting=int(record["muting"] or 0),
        reporting=int(record["reporting"] or 0),
        flagged_by_observer=bool(record["flagged_by_observer"]),
        flagged_count=int(record["flagged_count"] or 0),
        tier=record["tier"],
    )


def _verified(param: str) -> str:
    """Verified against the Cypher parameter `param`: strict `>`, to match
    GrapeRank's countTrustedRaters."""
    return f"__INF__ IS NOT NULL AND __INF__ > ${param}"


# Among subjects with an influence, these two are exact complements; a subject
# with no influence property at all is neither. The tier buckets partition a
# section across the three, so any drift leaves subjects sitting exactly on the
# line in no bucket at all.
_VERIFIED_LINE = _verified("verified_line")
_UNVERIFIED_LINE = "__INF__ IS NOT NULL AND __INF__ <= $verified_line"
_NO_INFLUENCE = "__INF__ IS NULL"

_TIER_PREDICATES: dict[str, str] = {
    # Bucket names match the GR result writer's count_values keys (see
    # message_queue_consumer.py). Placeholders:
    #   __INF__ → other[$influence_key]
    #   __TR__  → coalesce(other[$trusted_reporters_key], 0)
    # Parameter names $tier_high/$tier_medium_high/$tier_medium are the upper
    # bounds of high/medium_high/medium respectively — kept stable as API
    # surface, the semantic boundary value doesn't change with renaming.
    #
    # Bucketing is a fallthrough off the verified line: the fixed bands apply
    # only above it, and at or below it a subject is low (or flagged) whatever
    # a band would have said. Both /stats (get_all_section_stats) and
    # /connections?tier=… expand this one table, so a `tier` filter returns
    # exactly the rows /stats counted in that bucket.
    "high": f"{_VERIFIED_LINE} AND __INF__ >= $tier_high",
    "medium_high": (
        f"{_VERIFIED_LINE} AND __INF__ >= $tier_medium_high AND __INF__ < $tier_high"
    ),
    "medium": (
        f"{_VERIFIED_LINE} AND __INF__ >= $tier_medium AND __INF__ < $tier_medium_high"
    ),
    "medium_low": f"{_VERIFIED_LINE} AND __INF__ < $tier_medium",
    "low": f"({_NO_INFLUENCE} OR ({_UNVERIFIED_LINE} AND __TR__ < 2))",
    FLAGGED_TIER: f"{_UNVERIFIED_LINE} AND __TR__ >= 2",
}


def _expand(predicate: str, node: str = "other") -> str:
    """Bind the __INF__ / __TR__ placeholders to a node."""
    return (
        "("
        + predicate.replace("__INF__", f"{node}[$influence_key]").replace(
            "__TR__", f"coalesce({node}[$trusted_reporters_key], 0)"
        )
        + ")"
    )


def _count_where(predicate: str) -> str:
    return f"count(CASE WHEN {predicate} THEN 1 END)"


def _build_tier_predicate(tier: str | None) -> str:
    if not tier or tier not in _TIER_PREDICATES:
        return ""
    return _expand(_TIER_PREDICATES[tier])


def _tier_band_params() -> dict[str, float]:
    """The band bounds as Cypher params. Constants, never caller input: every
    `_TIER_PREDICATES` expander binds the same three, so /stats counts, the
    `?tier=` filter and a row's `tier` can't disagree on where a band starts."""
    return {
        "tier_high": TIER_HIGH,
        "tier_medium_high": TIER_MEDIUM_HIGH,
        "tier_medium": TIER_MEDIUM,
    }


def _tier_case(node: str = "other") -> str:
    """A node's tier, off the same `_TIER_PREDICATES` the counts and the
    `?tier=` filter expand, so a row can't name a bucket it isn't counted in.
    Arms are mutually exclusive; ELSE null only guards a future gap."""
    arms = " ".join(
        f"WHEN {_expand(predicate, node)} THEN '{name}'"
        for name, predicate in _TIER_PREDICATES.items()
    )
    return f"CASE {arms} ELSE null END"


def _row_to_item(row: dict) -> UserConnectionItem:
    return UserConnectionItem(
        pubkey=row["pubkey"],
        influence=row["influence"],
        trusted_reporters=(
            int(row["trusted_reporters"])
            if row["trusted_reporters"] is not None
            else None
        ),
        tier=row["tier"],
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
    # Keyword-only: two same-typed cutoffs are too easy to swap positionally,
    # and a swap is silently wrong rather than a TypeError. No defaults either —
    # forgetting them should fail, not serve DEFAULT's line to another observer.
    *,
    verified_cutoff: float,
    verified_line: float,
    order: str = "desc",
    tier: str | None = None,
    verified_only: bool = False,
    with_total: bool = False,
) -> tuple[list[UserConnectionItem], tuple[float, str] | None, int | None]:
    """Cursor-paginated connection list ordered by (influence <order>, pubkey ASC),
    optionally filtered to a single tier and/or to verified subjects only.

    Two distinct bars. `verified_cutoff` is THIS section's cutoff
    (`cutoffs.for_kind`: muter for muted_by, reporter for reported_by, follower
    otherwise), strict `>`, and drives `verified_only`. `verified_line` is always
    the follower cutoff — the tier ladder's low/unverified boundary — and drives
    each row's `tier`. They differ for muted_by/reported_by, so a DEFAULT muter
    at 0.015 passes `verified_only` (clears 0.01) yet is tiered `low` (under
    0.02); collapsing them is what made muter counts disagree with the TA.

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

    # Bound unconditionally: the per-row `verified` / `tier` columns reference
    # them whether or not the caller filters on them.
    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "trusted_reporters_key": trusted_reporters_key,
        "limit": limit,
        "verified_cutoff": verified_cutoff,
        "verified_line": verified_line,
        **_tier_band_params(),
    }
    verified_pred = _expand(_verified("verified_cutoff"))
    # Filter predicates (tier / verified_only) are cursor-independent and shared
    # by the page subquery and the optional count subquery. The cursor predicate
    # applies to the page only.
    filter_parts: list[str] = []
    tier_pred = _build_tier_predicate(tier)
    if tier_pred:
        filter_parts.append(tier_pred)
    if verified_only:
        filter_parts.append(verified_pred)

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
            tier: {_tier_case()},
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

    items = [_row_to_item(row) for row in rows]

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
    verified_line: float,
    limit: int,
    cursor_inf: float | None,
    cursor_pk: str | None,
    order: str = "desc",
    with_total: bool = False,
) -> tuple[list[UserConnectionItem], tuple[float, str] | None, int | None]:
    """DISTINCT flagged users across any relationship to `pubkey`. A user is
    flagged when (from `pubkey`'s perspective) they are not verified — influence
    at or below `verified_line`, the complement of the strict `>` used
    everywhere else — AND at least 2 trusted reporters have reported them.
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
        "verified_line": verified_line,
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
          AND other[$influence_key] <= $verified_line
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
          AND other[$influence_key] <= $verified_line
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
    # Every row matched the flagged predicate — the exact complement of
    # verified — so neither needs recomputing per row.
    items = [
        _row_to_item({**row, "tier": FLAGGED_TIER}) for row in rows
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
    verified_cutoff_by_kind: dict[str, float],
    verified_line: float,
) -> dict[str, ConnectionStats]:
    """Single-query version of get_section_stats covering all 6 relationships.
    ~20% faster than 6 parallel sessions on heavy accounts (1 round-trip).

    Each section's `verified` uses its own cutoff from `verified_cutoff_by_kind`,
    strict `>` to match GrapeRank. The tier buckets expand `_TIER_PREDICATES` —
    the same table `/connections?tier=…` filters on — so the two endpoints can't
    disagree about which subject sits in which bucket."""
    blocks: list[str] = ["MATCH (user:NostrUser {pubkey: $pubkey})"]
    return_fields: list[str] = []
    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "trusted_reporters_key": trusted_reporters_key,
        "verified_line": verified_line,
        **_tier_band_params(),
    }
    for name, rel_type, direction in _STATS_KINDS:
        pattern = _scoped_match_pattern(rel_type, direction)
        params[f"verified_cutoff_{name}"] = verified_cutoff_by_kind[name]
        counts = [
            "count(*) AS " + f"{name}_total",
            _count_where(_expand(_verified(f"verified_cutoff_{name}")))
            + f" AS {name}_verified",
            *(
                _count_where(_expand(predicate)) + f" AS {name}_{tier}"
                for tier, predicate in _TIER_PREDICATES.items()
            ),
        ]
        blocks.append(
            f"""
        CALL (user) {{
            MATCH {pattern}
            RETURN {", ".join(counts)}
        }}"""
        )
        return_fields.extend(
            f"{name}_{suffix}"
            for suffix in ("total", "verified", *_TIER_PREDICATES)
        )
    blocks.append("RETURN " + ", ".join(return_fields))
    query = "\n".join(blocks)

    result = await session.run(query, **params)
    record = await result.single()

    def _stats_for(name: str) -> ConnectionStats:
        if record is None:
            return ConnectionStats(
                total=0,
                verified=0,
                tier_counts=ConnectionTierCounts(
                    **{tier: 0 for tier in _TIER_PREDICATES}
                ),
            )
        return ConnectionStats(
            total=int(record[f"{name}_total"] or 0),
            verified=int(record[f"{name}_verified"] or 0),
            tier_counts=ConnectionTierCounts(
                **{
                    tier: int(record[f"{name}_{tier}"] or 0)
                    for tier in _TIER_PREDICATES
                }
            ),
        )

    return {name: _stats_for(name) for name, _, _ in _STATS_KINDS}


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


# ------------------------------ network alerts ------------------------------
#
# A network alert is a pubkey carrying more verified reports than its reach
# justifies. The bar scales with audience size so a large account isn't flagged
# by the same absolute report count as a small one:
#
#     N     = 2 + floor(verified_follower_count / 500)
#     alert = verified_reporter_count > N
#
# The work is split across three bounded queries rather than one big one,
# because the only expensive input is the verified follower count:
#
#   1. get_network_alert_candidates    — property reads only, no edge walks
#   2. count_above_cutoff_followers_capped — counts followers, capped; used for
#                                        any candidate with no stored count
#   3. count_verified_muters           — display-only, over final rows only
#
# NOTHING IN THIS MODULE — OR ANYWHERE ELSE IN THE REPO TODAY — WRITES
# `trusted_followers_<observer>`. Step (1) reads it, but the GrapeRank result
# writer currently drops that scorecard field, so it is absent on every node and
# step (2) runs for every candidate. The read is deliberate: persisting the
# property turns step (2) into a property lookup, which is a follow-up change
# with its own tradeoffs (extra per-observer property, faster property-key token
# growth). Until then this path is read-only against Neo4j and pays a bounded
# follower traversal per candidate instead.
#
# Two things keep step (1) off a full label scan:
#
#   Anchoring. Candidates are reached *through* the REPORTS edge instead of by
#   scanning :NostrUser. Only a reported pubkey can clear N >= 2, and the
#   reported set is orders of magnitude smaller than the node count. (There is
#   no index that could help instead: the influence/reporter properties are
#   per-observer, so indexing them would mean one index per observer.)
#
#   Prefiltering. The floor term is non-negative, so N >= 2 for everyone and
#   `verified_reporter_count >= 3` is a superset of every possible alert. It
#   applies before any arithmetic without dropping a qualifying row.
#
# Section membership uses the live (observer)-[:FOLLOWS]->(bob) edge, NOT
# `hops_<observer> = 1`. The edge is maintained by kind-3 ingest (seconds
# behind the user's follow list); `hops_` is only rewritten by a GrapeRank run,
# so it would keep alerting on people the observer already unfollowed. `hops_`
# is still returned for display, and remains the right input for a future
# "alert me about pubkeys X hops out" control.

# Grain of the threshold: one extra tolerated report per this many verified
# followers. Shared with the service so the two can't drift apart.
FOLLOWERS_PER_EXTRA_REPORT = 500

# Ceiling on candidates pulled back from step (1). Far above any plausible
# real count — a backstop against a pathological graph, not a paging limit.
# Callers are expected to log when it bites rather than truncate silently.
MAX_ALERT_CANDIDATES = 5000

_ALERT_CANDIDATES_QUERY = """
MATCH (observer:NostrUser {pubkey: $observer_pubkey})
CALL (observer) {
    MATCH (:NostrUser)-[:REPORTS]->(bob:NostrUser)
    WITH DISTINCT observer, bob
    WHERE coalesce(bob[$trusted_reporters_key], 0) >= 3
      AND bob.pubkey <> observer.pubkey
    WITH bob,
         toInteger(coalesce(bob[$trusted_reporters_key], 0)) AS verified_reporters,
         bob[$trusted_followers_key] AS stored_followers,
         bob[$influence_key] AS influence,
         bob[$hops_key] AS hops,
         EXISTS { (observer)-[:FOLLOWS]->(bob) } AS is_direct
    WHERE is_direct OR (influence IS NOT NULL AND influence > $cutoff)
    RETURN bob.pubkey AS pubkey, verified_reporters, stored_followers,
           influence, hops, is_direct
}
RETURN pubkey, verified_reporters, stored_followers, influence, hops, is_direct
LIMIT $max_candidates
"""


async def get_network_alert_candidates(
    session: AsyncNeoDriver,
    observer_pubkey: str,
    influence_key: str,
    hops_key: str,
    trusted_followers_key: str,
    trusted_reporters_key: str,
    cutoff: float,
    max_candidates: int = MAX_ALERT_CANDIDATES,
) -> list[dict]:
    """Pubkeys that could be network alerts, before the report threshold applies.

    Reachability is already decided here (directly followed, or above `cutoff`),
    but the threshold is NOT — it needs a verified follower count, and
    `stored_followers` comes back None when this observer has no GrapeRank run
    carrying `trusted_followers_<observer>` yet. The caller resolves those.

    Touches no follower or muter edges: every value is a property read on the
    candidate node itself.
    """
    result = await session.run(
        _ALERT_CANDIDATES_QUERY,
        observer_pubkey=observer_pubkey,
        influence_key=influence_key,
        hops_key=hops_key,
        trusted_followers_key=trusted_followers_key,
        trusted_reporters_key=trusted_reporters_key,
        cutoff=cutoff,
        max_candidates=int(max_candidates),
    )
    return [dict(record) async for record in result]


async def count_above_cutoff_followers_capped(
    session: AsyncNeoDriver,
    pubkeys: list[str],
    influence_key: str,
    cutoff: float,
    cap: int,
) -> dict[str, int]:
    """Above-cutoff follower counts, each stopping at `cap`.

    Fallback for candidates with no stored `trusted_followers_<observer>`.

    `cap` is what makes this safe to run on a huge account. A row can only clear
    the threshold when its verified follower count is below
    `FOLLOWERS_PER_EXTRA_REPORT * (verified_reporters - 2)`, so counting past
    that point cannot change any decision. Pass that value as `cap` and read the
    result as: **count < cap → exact count, row passes; count == cap → the true
    count is at least `cap`, row fails.** A row that survives therefore always
    carries a real number, never a truncated one.

    Caveat worth knowing: `cap` bounds the above-cutoff followers *collected*,
    not the follower edges *scanned*. An account with many followers but few
    above the cutoff never reaches the cap and still costs a full traversal of
    its follower edges. The cap helps most where the risk is highest (accounts
    with a large trusted following) and never hurts.

    Cypher forbids a LIMIT that varies per row, so callers bucket pubkeys by
    identical `cap` and call this once per bucket; `cap` is interpolated and so
    is guarded here, at the interpolation site.
    """
    if type(cap) is not int or not 1 <= cap <= 10_000_000:
        raise ValueError(f"cap must be an int in [1, 10000000], got {cap!r}")
    if not pubkeys:
        return {}

    query = f"""
    UNWIND $pubkeys AS pk
    MATCH (bob:NostrUser {{pubkey: pk}})
    CALL (bob) {{
        MATCH (f:NostrUser)-[:FOLLOWS]->(bob)
        WHERE f[$influence_key] IS NOT NULL
          AND f[$influence_key] >= $cutoff
        WITH f
        LIMIT {cap}
        RETURN count(f) AS capped_followers
    }}
    RETURN pk AS pubkey, capped_followers
    """
    result = await session.run(
        query, pubkeys=pubkeys, influence_key=influence_key, cutoff=cutoff
    )
    return {
        record["pubkey"]: int(record["capped_followers"] or 0)
        async for record in result
    }


async def count_verified_muters(
    session: AsyncNeoDriver,
    pubkeys: list[str],
    influence_key: str,
    verified_threshold: float,
) -> dict[str, int]:
    """Above-threshold muter counts, keyed by pubkey.

    Display-only — the algorithm never computes a `trusted_muters` scorecard
    field, so there is nothing stored to read. Never part of the filter, so
    callers run it last, over the already-limited result rows.
    """
    if not pubkeys:
        return {}

    query = """
    UNWIND $pubkeys AS pk
    MATCH (bob:NostrUser {pubkey: pk})
    CALL (bob) {
        MATCH (m:NostrUser)-[:MUTES]->(bob)
        WHERE m[$influence_key] IS NOT NULL
          AND m[$influence_key] >= $verified_threshold
        RETURN count(m) AS verified_muters
    }
    RETURN pk AS pubkey, verified_muters
    """
    result = await session.run(
        query,
        pubkeys=pubkeys,
        influence_key=influence_key,
        verified_threshold=verified_threshold,
    )
    return {
        record["pubkey"]: int(record["verified_muters"] or 0)
        async for record in result
    }
