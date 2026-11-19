from typing import Any, NamedTuple

from neo4j import AsyncManagedTransaction
from neo4j import AsyncSession as AsyncNeoSession
from neo4j import Query
from neo4j.exceptions import ClientError

from app.core.config import settings
from app.core.tier_thresholds import (
    FLAGGED_TIER,
    TIER_HIGH,
    TIER_MEDIUM,
    TIER_MEDIUM_HIGH,
    classify_tier,
)
from app.repos.scorecard_repo import (
    Members,
    Score,
    count_members,
    page_of_members,
    scores_by_id,
    scores_by_pubkey,
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
    session: AsyncNeoSession,
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

    if settings.scores_in_postgres:
        return await _connections_from_scorecards(
            session, match_clause, pubkey, observer or pubkey
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


async def _connections_from_scorecards(
    session: AsyncNeoSession, match_clause: str, pubkey: str, observer: str
) -> list[UserConnection]:
    """Graph first: the graph says who (by User id), the Scorecard database gives
    each one's pubkey and Influence. A node without an id keeps its pubkey and
    has no score."""
    result = await session.run(
        f"""
        MATCH {match_clause}
        RETURN other.user_id AS id,
            CASE WHEN other.user_id IS NULL THEN other.pubkey END AS pubkey
        """,
        pubkey=pubkey,
    )
    rows = [(record["id"], record["pubkey"]) async for record in result]
    scored = await scores_by_id(observer, [uid for uid, _ in rows if uid is not None])
    return [
        UserConnection(pubkey=pk, influence=None)
        if uid is None
        else UserConnection(pubkey=scored[uid][0], influence=scored[uid][1].influence)
        for uid, pk in rows
    ]


# ----------------- Refactored Functions -----------------


# Follows
async def get_list_of_pubkeys_following_user(
    session: AsyncNeoSession, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="FOLLOWS", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_follows(
    session: AsyncNeoSession, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="FOLLOWS", direction="outgoing"
    )


# Mutes
async def get_list_of_pubkeys_muting_user(
    session: AsyncNeoSession, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="MUTES", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_mutes(
    session: AsyncNeoSession, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="MUTES", direction="outgoing"
    )


# Reports
async def get_list_of_pubkeys_reporting_user(
    session: AsyncNeoSession, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="REPORTS", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_reports(
    session: AsyncNeoSession, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="REPORTS", direction="outgoing"
    )


# ----------------- Follows -----------------


# Number of users following a given pubkey
async def count_following_user(session: AsyncNeoSession, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:FOLLOWS]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of other users a given pubkey follows. A self-follow doesn't count: the
# worker's reach excludes the observer, so it leaves nobody to score.
async def count_user_follows(session: AsyncNeoSession, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:FOLLOWS]->(other:NostrUser)
    WHERE other <> user
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Which of the given pubkeys follow at least one other user (i.e. are schedulable).
async def pubkeys_following_someone(
    session: AsyncNeoSession, pubkeys: list[str]
) -> set[str]:
    if not pubkeys:
        return set()
    query = """
    MATCH (user:NostrUser)-[:FOLLOWS]->(other:NostrUser)
    WHERE user.pubkey IN $pubkeys AND other <> user
    RETURN DISTINCT user.pubkey AS pubkey
    """
    result = await session.run(query, pubkeys=pubkeys)
    return {record["pubkey"] async for record in result}


# ----------------- Mutes -----------------


# Number of users muting a given pubkey
async def count_muting_user(session: AsyncNeoSession, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:MUTES]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey mutes
async def count_user_mutes(session: AsyncNeoSession, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:MUTES]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# ----------------- Reports -----------------


# Number of users reporting a given pubkey
async def count_reporting_user(session: AsyncNeoSession, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:REPORTS]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey reports
async def count_user_reports(session: AsyncNeoSession, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:REPORTS]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


async def get_influence_for_observer(
    session: AsyncNeoSession, pubkey: str, observer_pubkey: str
) -> float | None:
    """
    Returns the value of 'influence_<observer_pubkey>' for the NostrUser with pubkey.
    Returns None if the user or property does not exist.
    """
    if settings.scores_in_postgres:
        scored = await scores_by_pubkey(observer_pubkey, [pubkey])
        return scored[pubkey].influence if pubkey in scored else None

    property_name = f"influence_{observer_pubkey}"
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})
    RETURN user[$property_name] AS influence
    """

    result = await session.run(query, pubkey=pubkey, property_name=property_name)
    record = await result.single()
    return record["influence"] if record and record["influence"] is not None else None


async def get_qualifying_asserters_for_observer(
    session: AsyncNeoSession,
    pubkeys: list[str],
    observer_pubkey: str,
    min_influence: float,
) -> dict[str, float]:
    """Of `pubkeys`, those whose Influence in `observer_pubkey`'s web of trust
    clears `min_influence` (inclusive), mapped to their trust weight.

    The weight is the asserter's Influence quantized to the Rank quantum —
    `round(influence * 100) / 100`. Tapestry derives the same weight as
    `wot_rank_<pov> / 100` (`src/api/profile-tags/index.js:679`), and Rank is
    `round(Influence * 100)` (CONTEXT.md), so quantizing here is what makes the
    two implementations agree exactly rather than merely closely.

    One round trip for the whole set — the per-pubkey `get_influence_for_observer`
    would be N queries during a Trusted List run. The observer key is passed as a
    VALUE via `node[$property_name]`, never f-string-interpolated, matching
    `_get_pubkeys_with_influence`.

    A pubkey with no node, or no influence property for this observer, is absent
    from the result: unscored is NOT treated as zero-and-below-threshold, it is
    simply not qualifying. Callers must not read an empty list as "everyone
    failed" without checking whether the observer has been scored at all.
    """
    if not pubkeys:
        return {}
    if settings.scores_in_postgres:
        scored = await scores_by_pubkey(observer_pubkey, pubkeys)
        return {
            pk: round(float(score.influence) * 100) / 100
            for pk, score in scored.items()
            if score.influence is not None and score.influence >= min_influence
        }
    property_name = f"influence_{observer_pubkey}"
    query = """
    UNWIND $pubkeys AS pk
    MATCH (user:NostrUser {pubkey: pk})
    WITH pk, user[$property_name] AS influence
    WHERE influence IS NOT NULL AND influence >= $min_influence
    RETURN pk, influence
    """
    result = await session.run(
        query,
        pubkeys=pubkeys,
        property_name=property_name,
        min_influence=min_influence,
    )
    return {
        record["pk"]: round(float(record["influence"]) * 100) / 100
        async for record in result
    }


# ----------------- overview / stats / paginated connections -----------------
#
# Notes on the Cypher patterns below:
# - Match pattern direction "in" = (other)-[:REL]->(user); "out" = (user)-[:REL]->(other).
# - Read a node's Influence once per row (`_bind_score`) and reuse the bound
#   name: every `other[$influence_key]` walks the node's whole property list.
# - Never alias a value as `inf`, `infinity` or `nan`: Cypher reads those as the
#   float literals, so `WITH x AS inf` yields Infinity on every row.


def _scoped_match_pattern(rel_type: str, direction: str) -> str:
    """MATCH pattern assuming `user` is already bound in the surrounding scope
    (e.g. inside a `CALL (user)` subquery)."""
    if direction == "in":
        return f"(other:NostrUser)-[:{rel_type}]->(user)"
    return f"(user)-[:{rel_type}]->(other:NostrUser)"


_INFLUENCE_PREFIX = "influence_"


def _observer_of(influence_key: str) -> str:
    """The Observer pubkey behind a Neo4j `influence_<observer>` property name."""
    if not influence_key.startswith(_INFLUENCE_PREFIX):
        raise ValueError(f"not an influence key: {influence_key!r:.80}")
    return influence_key[len(_INFLUENCE_PREFIX) :]


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
    session: AsyncNeoSession,
    pubkey: str,
    influence_key: str,
) -> OutboundCounts:
    """Influence + outbound counts, and nothing the verified line touches.

    The lean half of `get_outbound_counts_and_influence`, for callers surfacing
    neither the flagged fields nor verified/tier — currently ORE-02. Skips the
    flagged DISTINCT scan over every inbound edge, and takes no line, so the
    caller needs no preset read. Need a line-driven field? Use the full query.
    """
    in_postgres = settings.scores_in_postgres
    influence = "null" if in_postgres else "user[$influence_key]"
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}}){_OUTBOUND_COUNT_BLOCKS}
    RETURN {influence} AS influence, following, muting, reporting
    """
    result = await session.run(query, pubkey=pubkey, influence_key=influence_key)
    record = await result.single()
    if not record:
        return OutboundCounts(None, 0, 0, 0)
    stored = record["influence"]
    if in_postgres:
        scored = await scores_by_pubkey(_observer_of(influence_key), [pubkey])
        stored = scored[pubkey].influence if pubkey in scored else None
    return OutboundCounts(
        influence=stored,
        following=int(record["following"] or 0),
        muting=int(record["muting"] or 0),
        reporting=int(record["reporting"] or 0),
    )


async def get_outbound_counts_and_influence(
    session: AsyncNeoSession,
    pubkey: str,
    influence_key: str,
    *,
    verified_line: float,
    reporter_cutoff: float,
) -> OutboundOverview:
    """One round-trip: the user's own influence and tier, plus outbound counts,
    flagged_by_observer and a DISTINCT flagged_count across all relationships.

    The user's own tier uses the same predicates the section rows do, against
    the follower cutoff (`verified_line`) — the general trusted bar, since the
    subject isn't rating anyone here.

    "Flagged" is *not verified* (no stored influence, or `<= verified_line`: the
    complement of the strict `>` used everywhere else) AND reported by 2+ accounts
    above `reporter_cutoff`, counted live."""
    if settings.scores_in_postgres:
        return await _outbound_overview_from_scorecards(
            session,
            pubkey,
            _observer_of(influence_key),
            verified_line=verified_line,
            reporter_cutoff=reporter_cutoff,
        )
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}}){_OUTBOUND_COUNT_BLOCKS}
    CALL (user) {{
        MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)
        {_bind_score()}
        WHERE {_expand(_TIER_PREDICATES[FLAGGED_TIER])}
        RETURN count(DISTINCT other) AS flagged_count
    }}
    {_bind_score("user", "following", "muting", "reporting", "flagged_count")}
    RETURN
        {_score("user")} AS influence,
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
        verified_line=verified_line,
        reporter_cutoff=reporter_cutoff,
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


# Who reports `node`, by User id. A reporter without an id has no score.
def _reporter_ids(node: str) -> str:
    return f"[(rr:NostrUser)-[:REPORTS]->({node}) | rr.user_id]"


def _live_reporter_count(
    reporters: list[int | None],
    scored: dict[int, tuple[str, Score]],
    reporter_cutoff: float,
) -> int:
    """`_live_reporters` over Scorecards: reporters strictly above the cutoff."""
    count = 0
    for uid in reporters:
        influence = scored[uid][1].influence if uid is not None else None
        if influence is not None and influence > reporter_cutoff:
            count += 1
    return count


def _ids(*lists: list[int | None]) -> list[int]:
    return [uid for ids in lists for uid in ids if uid is not None]


async def _outbound_overview_from_scorecards(
    session: AsyncNeoSession,
    pubkey: str,
    observer: str,
    *,
    verified_line: float,
    reporter_cutoff: float,
) -> OutboundOverview:
    """`get_outbound_counts_and_influence`, graph first. Only a neighbour with
    2+ reporters can be flagged, so only those leave the graph."""
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}}){_OUTBOUND_COUNT_BLOCKS}
    CALL (user) {{
        MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)
        WITH DISTINCT other
        WITH other, {_reporter_ids("other")} AS reporters
        WHERE size(reporters) >= 2
        RETURN collect({{id: other.user_id, reporters: reporters}}) AS candidates
    }}
    RETURN following, muting, reporting, candidates,
        {_reporter_ids("user")} AS own_reporters
    """
    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    if not record:
        return OutboundOverview(None, 0, 0, 0, False, 0, None)
    candidates = record["candidates"]
    own_reporters = record["own_reporters"]
    scored = await scores_by_id(
        observer,
        _ids(
            [c["id"] for c in candidates],
            own_reporters,
            *(c["reporters"] for c in candidates),
        ),
    )
    own = await scores_by_pubkey(observer, [pubkey])
    influence = own[pubkey].influence if pubkey in own else None

    flagged_count = 0
    for candidate in candidates:
        uid = candidate["id"]
        other = scored[uid][1].influence if uid is not None else None
        live = _live_reporter_count(candidate["reporters"], scored, reporter_cutoff)
        if classify_tier(other, live, verified_line) == FLAGGED_TIER:
            flagged_count += 1
    tier = classify_tier(
        influence,
        _live_reporter_count(own_reporters, scored, reporter_cutoff),
        verified_line,
    )
    return OutboundOverview(
        influence=influence,
        following=int(record["following"] or 0),
        muting=int(record["muting"] or 0),
        reporting=int(record["reporting"] or 0),
        flagged_by_observer=tier == FLAGGED_TIER,
        flagged_count=flagged_count,
        tier=tier,
    )


class TrustSignalRow(NamedTuple):
    influence: float | None
    verified: bool
    flagged: bool


async def get_trust_signals_for_pubkeys(
    session: AsyncNeoSession,
    pubkeys: list[str],
    influence_key: str,
    *,
    verified_line: float,
    reporter_cutoff: float,
) -> dict[str, TrustSignalRow]:
    """`/overview`'s verdicts for many subjects in one UNWIND; unknown pubkeys unrated."""
    if not pubkeys:
        return {}
    if settings.scores_in_postgres:
        return await _trust_signals_from_scorecards(
            session,
            pubkeys,
            _observer_of(influence_key),
            verified_line=verified_line,
            reporter_cutoff=reporter_cutoff,
        )
    query = f"""
    UNWIND $pubkeys AS pk
    OPTIONAL MATCH (user:NostrUser {{pubkey: pk}})
    {_bind_score("user", "pk")}
    RETURN
        pk AS pubkey,
        {_score("user")} AS influence,
        coalesce({_expand(_VERIFIED_LINE, "user")}, false) AS verified,
        coalesce({_expand(_TIER_PREDICATES[FLAGGED_TIER], "user")}, false) AS flagged
    """
    result = await session.run(
        query,
        pubkeys=pubkeys,
        influence_key=influence_key,
        verified_line=verified_line,
        reporter_cutoff=reporter_cutoff,
    )
    out = {pk: TrustSignalRow(None, False, False) for pk in pubkeys}
    async for record in result:
        out[record["pubkey"]] = TrustSignalRow(
            influence=record["influence"],
            verified=bool(record["verified"]),
            flagged=bool(record["flagged"]),
        )
    return out


async def _trust_signals_from_scorecards(
    session: AsyncNeoSession,
    pubkeys: list[str],
    observer: str,
    *,
    verified_line: float,
    reporter_cutoff: float,
) -> dict[str, TrustSignalRow]:
    """`get_trust_signals_for_pubkeys`, graph first: the graph gives each
    subject's reporters, the Scorecard database every score."""
    result = await session.run(
        f"""
        UNWIND $pubkeys AS pk
        MATCH (user:NostrUser {{pubkey: pk}})
        RETURN pk AS pubkey, {_reporter_ids("user")} AS reporters
        """,
        pubkeys=pubkeys,
    )
    reporters = {record["pubkey"]: record["reporters"] async for record in result}
    own = await scores_by_pubkey(observer, pubkeys)
    scored = await scores_by_id(observer, _ids(*reporters.values()))
    out = {}
    for pk in pubkeys:
        influence = own[pk].influence if pk in own else None
        live = _live_reporter_count(reporters.get(pk, []), scored, reporter_cutoff)
        out[pk] = TrustSignalRow(
            influence=influence,
            verified=influence is not None and influence > verified_line,
            flagged=classify_tier(influence, live, verified_line) == FLAGGED_TIER,
        )
    return out


def _verified(param: str) -> str:
    """Verified against the Cypher parameter `param`: strict `>`, to match
    GrapeRank's countTrustedRaters."""
    return f"__INF__ IS NOT NULL AND __INF__ > ${param}"


# Exact complements: no stored influence (out of reach, or at or below the
# worker's persist floor) counts as not verified. Let them drift and a subject
# sitting exactly on the line lands in no bucket.
_VERIFIED_LINE = _verified("verified_line")
_UNVERIFIED_LINE = "(__INF__ IS NULL OR __INF__ <= $verified_line)"

_TIER_PREDICATES: dict[str, str] = {
    # Bucket names match the GR result writer's count_values keys (see
    # message_queue_consumer.py). Placeholders:
    #   __INF__ → the node's bound Influence (`_bind_score`)
    #   __TR__  → the live verified-reporter count of `other`
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
    "low": f"{_UNVERIFIED_LINE} AND __TR__ < 2",
    FLAGGED_TIER: f"{_UNVERIFIED_LINE} AND __TR__ >= 2",
}


def _score(node: str = "other") -> str:
    """The name `_bind_score` gives `node`'s Influence."""
    return f"{node}_score"


def _bind_score(node: str = "other", *keep: str) -> str:
    """A WITH that reads `node`'s Influence once; `keep` stays in scope with it."""
    kept = ", ".join((node, *keep))
    return f"WITH {kept}, {node}[$influence_key] AS {_score(node)}"


def _live_reporters(node: str = "other") -> str:
    """Reporters of `node` strictly above the observer's preset reporter cutoff."""
    return (
        f"COUNT {{ (rr:NostrUser)-[:REPORTS]->({node}) "
        "WHERE rr[$influence_key] > $reporter_cutoff }"
    )


def _expand(predicate: str, node: str = "other") -> str:
    """Bind the __INF__ / __TR__ placeholders to a node whose Influence the
    query has already bound with `_bind_score`."""
    return (
        "("
        + predicate.replace("__INF__", _score(node)).replace(
            "__TR__", _live_reporters(node)
        )
        + ")"
    )


def _count_where(predicate: str) -> str:
    return f"count(CASE WHEN {predicate} THEN 1 END)"


def _build_tier_predicate(tier: str | None) -> str:
    if not tier or tier not in _TIER_PREDICATES:
        return ""
    return _expand(_TIER_PREDICATES[tier])


def _tier_band_params() -> dict[str, Any]:
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


def _row_map(*extra: str) -> str:
    """A connection row's Cypher map over `other`, plus any `extra` fields.
    `other`'s Influence must already be bound."""
    fields = [
        "pubkey: other.pubkey",
        f"influence: {_score()}",
        f"trusted_reporters: {_live_reporters()}",
        *extra,
    ]
    return "{" + ", ".join(fields) + "}"


def _order_by(order: str) -> str:
    return f"ORDER BY sort_inf {'ASC' if order == 'asc' else 'DESC'}, other.pubkey ASC"


def _after_cursor(order: str, inf: str = "sort_inf", pk: str = "other.pubkey") -> str:
    """Rows strictly after ($cursor_inf, $cursor_pk) in `order`; ties step by pubkey ASC."""
    cmp = ">" if order == "asc" else "<"
    return f"({inf} {cmp} $cursor_inf OR ({inf} = $cursor_inf AND {pk} > $cursor_pk))"


def _cursor_params(cursor_inf: float | None, cursor_pk: str | None) -> dict:
    """Both bound, or both null (no cursor)."""
    if cursor_inf is None or cursor_pk is None:
        return {"cursor_inf": None, "cursor_pk": None}
    return {"cursor_inf": cursor_inf, "cursor_pk": cursor_pk}


def _flagged_single_pass_query(scan: str, where: str, row: str, order: str) -> str:
    """Flagged sets are small (>= 2 verified reports), so collect them once in
    sort order and take both the cursor page and the total from that list. The
    bound Influence rides along in the list, so the page does not read it again."""
    after = _after_cursor(order, "r.sort_inf", "r.pubkey")
    score = _score()
    return f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})
    CALL (user) {{
        {scan}
        {_bind_score()}
        WITH other, {score}, coalesce({score}, -1.0) AS sort_inf
        WHERE {where}
        WITH other, {score}, sort_inf
        {_order_by(order)}
        RETURN collect({{
            other: other, pubkey: other.pubkey, sort_inf: sort_inf, score: {score}
        }}) AS flagged
    }}
    WITH [r IN flagged WHERE $cursor_pk IS NULL OR {after}][..$limit] AS page,
         size(flagged) AS total
    CALL (page) {{
        UNWIND page AS r
        WITH r.other AS other, r.sort_inf AS sort_inf, r.score AS {score}
        RETURN collect({row}) AS rows
    }}
    RETURN rows, total
    """


# ----------------- graph first (SCORES_IN_POSTGRES) -----------------
#
# The graph says who is in a section: every member's User id in one collected
# list, and, for the few members anyone reports, their reporters' ids. The
# Scorecard database then tiers, filters, sorts and pages by the same rules the
# Cypher above applies (`scorecard_repo.page_of_members` / `count_members`).

_NO_ID_PUBKEY = "CASE WHEN other.user_id IS NULL THEN other.pubkey END"


def _members_blocks(scan: str, suffix: str = "") -> str:
    """One pass over `scan`: every member's id (collect drops the nulls, so a
    node without an id lands in `no_id` by pubkey), and the reported members
    with their reporters' ids."""
    return f"""
    CALL (user) {{
        {scan}
        WITH other, EXISTS {{ (:NostrUser)-[:REPORTS]->(other) }} AS is_reported
        RETURN collect(other.user_id) AS ids{suffix},
            collect({_NO_ID_PUBKEY}) AS no_id{suffix},
            collect(CASE WHEN is_reported THEN {{
                id: other.user_id, pk: {_NO_ID_PUBKEY}, r: {_reporter_ids("other")}
            }} END) AS reported{suffix}
    }}"""


async def _members_from(
    observer: str, record, suffixes: list[str], reporter_cutoff: float
) -> list[Members]:
    """`Members` per section from a `_members_blocks` record, with each reported
    member's live reporter count worked out from its reporters' scores."""
    if record is None:
        return [Members([], [], {}) for _ in suffixes]
    scored = await scores_by_id(
        observer,
        {
            uid
            for suffix in suffixes
            for reported in record[f"reported{suffix}"]
            for uid in reported["r"]
            if uid is not None
        },
    )
    sections = []
    for suffix in suffixes:
        live: dict[int | str, int] = {}
        for reported in record[f"reported{suffix}"]:
            count = _live_reporter_count(reported["r"], scored, reporter_cutoff)
            if count:
                key = reported["id"] if reported["id"] is not None else reported["pk"]
                live[key] = count
        sections.append(Members(record[f"ids{suffix}"], record[f"no_id{suffix}"], live))
    return sections


async def _section_connections_from_scorecards(
    session: AsyncNeoSession,
    scan: str,
    pubkey: str,
    observer: str,
    limit: int,
    cursor_inf: float | None,
    cursor_pk: str | None,
    *,
    verified_cutoff: float | None,
    verified_line: float,
    reporter_cutoff: float,
    order: str,
    tier: str | None,
    with_total: bool,
) -> tuple[list[UserConnectionItem], tuple[float, str] | None, int | None]:
    result = await session.run(
        f"""
        MATCH (user:NostrUser {{pubkey: $pubkey}}){_members_blocks(scan)}
        RETURN ids, no_id, reported
        """,
        pubkey=pubkey,
    )
    (members,) = await _members_from(
        observer, await result.single(), [""], reporter_cutoff
    )
    rows, total = await page_of_members(
        observer,
        members,
        verified_line=verified_line,
        order=order,
        limit=limit,
        cursor=(
            (cursor_inf, cursor_pk)
            if cursor_inf is not None and cursor_pk is not None
            else None
        ),
        tier=tier,
        verified_cutoff=verified_cutoff,
    )
    items = [
        UserConnectionItem(
            pubkey=row.pubkey,
            influence=row.influence,
            trusted_reporters=row.reporters,
            tier=row.tier,
        )
        for row in rows
    ]
    last_cursor = (
        (float(rows[-1].sort_inf), rows[-1].pubkey)
        if rows and len(rows) == limit
        else None
    )
    return items, last_cursor, total if with_total else None


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
    session: AsyncNeoSession,
    pubkey: str,
    influence_key: str,
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
    reporter_cutoff: float,
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
    if settings.scores_in_postgres:
        return await _section_connections_from_scorecards(
            session,
            f"MATCH {scoped_pattern}",
            pubkey,
            _observer_of(influence_key),
            limit,
            cursor_inf,
            cursor_pk,
            verified_cutoff=verified_cutoff if verified_only else None,
            verified_line=verified_line,
            reporter_cutoff=reporter_cutoff,
            order=order,
            tier=tier,
            with_total=with_total,
        )

    # Bound unconditionally: the per-row `verified` / `tier` columns reference
    # them whether or not the caller filters on them.
    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "limit": limit,
        "verified_cutoff": verified_cutoff,
        "verified_line": verified_line,
        "reporter_cutoff": reporter_cutoff,
        **_tier_band_params(),
        **_cursor_params(cursor_inf, cursor_pk),
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

    row = _row_map(f"tier: {_tier_case()}", "sort_inf: sort_inf")

    if tier == FLAGGED_TIER:
        query = _flagged_single_pass_query(
            scan=f"MATCH {scoped_pattern}",
            where=" AND ".join(filter_parts),
            row=row,
            order=order,
        )
    else:
        page_parts = list(filter_parts)
        if params["cursor_pk"] is not None:
            page_parts.append(_after_cursor(order))
        page_where = ("WHERE " + " AND ".join(page_parts)) if page_parts else ""
        # The plain total reads no score; a filtered one binds it first.
        count_where = (
            (_bind_score() + " WHERE " + " AND ".join(filter_parts))
            if filter_parts
            else ""
        )

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
        {_bind_score()}
        WITH other, {_score()}, coalesce({_score()}, -1.0) AS sort_inf
        {page_where}
        WITH other, {_score()}, sort_inf
        {_order_by(order)}
        LIMIT $limit
        RETURN collect({row}) AS rows
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
    session: AsyncNeoSession,
    pubkey: str,
    influence_key: str,
    limit: int,
    cursor_inf: float | None,
    cursor_pk: str | None,
    *,
    verified_line: float,
    reporter_cutoff: float,
    order: str = "desc",
    with_total: bool = False,
) -> tuple[list[UserConnectionItem], tuple[float, str] | None, int | None]:
    """DISTINCT flagged users across any relationship to `pubkey`. A user is
    flagged when (from `pubkey`'s perspective) they are not verified — no stored
    influence, or at or below `verified_line`, the complement of the strict `>`
    used everywhere else — AND at least 2 reporters above `reporter_cutoff`
    report them, counted live.
    Cursor-paginated by (influence <order>, pubkey ASC). Same shape as
    get_paginated_section_connections so the client can reuse one item type.
    One pass yields both the page and the DISTINCT flagged total; `with_total`
    only decides whether the total is returned.
    Returns (items, last_record_cursor_or_none, total_or_none)."""
    if settings.scores_in_postgres:
        return await _section_connections_from_scorecards(
            session,
            "MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)\n"
            "            WITH DISTINCT other",
            pubkey,
            _observer_of(influence_key),
            limit,
            cursor_inf,
            cursor_pk,
            verified_cutoff=None,
            verified_line=verified_line,
            reporter_cutoff=reporter_cutoff,
            order=order,
            tier=FLAGGED_TIER,
            with_total=with_total,
        )
    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "verified_line": verified_line,
        "reporter_cutoff": reporter_cutoff,
        "limit": limit,
        **_cursor_params(cursor_inf, cursor_pk),
    }
    query = _flagged_single_pass_query(
        scan=(
            "MATCH (other:NostrUser)-[:FOLLOWS|MUTES|REPORTS]-(user)\n"
            "        WITH DISTINCT other"
        ),
        where=_expand(_TIER_PREDICATES[FLAGGED_TIER]),
        row=_row_map("sort_inf: sort_inf"),
        order=order,
    )

    result = await session.run(query, **params)
    record = await result.single()
    rows = record["rows"] if record else []
    total = (int(record["total"]) if record else 0) if with_total else None
    # Every row matched the flagged predicate — the exact complement of
    # verified — so neither needs recomputing per row.
    items = [_row_to_item({**row, "tier": FLAGGED_TIER}) for row in rows]
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
    session: AsyncNeoSession,
    pubkey: str,
    influence_key: str,
    verified_cutoff_by_kind: dict[str, float],
    *,
    verified_line: float,
    reporter_cutoff: float,
) -> dict[str, ConnectionStats]:
    """Single-query version of get_section_stats covering all 6 relationships.
    ~20% faster than 6 parallel sessions on heavy accounts (1 round-trip).

    Each section's `verified` uses its own cutoff from `verified_cutoff_by_kind`,
    strict `>` to match GrapeRank. The tier buckets expand `_TIER_PREDICATES` —
    the same table `/connections?tier=…` filters on — so the two endpoints can't
    disagree about which subject sits in which bucket."""
    if settings.scores_in_postgres:
        return await _section_stats_from_scorecards(
            session,
            pubkey,
            _observer_of(influence_key),
            verified_cutoff_by_kind,
            verified_line=verified_line,
            reporter_cutoff=reporter_cutoff,
        )
    blocks: list[str] = ["MATCH (user:NostrUser {pubkey: $pubkey})"]
    return_fields: list[str] = []
    params: dict = {
        "pubkey": pubkey,
        "influence_key": influence_key,
        "verified_line": verified_line,
        "reporter_cutoff": reporter_cutoff,
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
            {_bind_score()}
            RETURN {", ".join(counts)}
        }}"""
        )
        return_fields.extend(
            f"{name}_{suffix}" for suffix in ("total", "verified", *_TIER_PREDICATES)
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


async def _members_of_every_section(
    session: AsyncNeoSession, pubkey: str, observer: str, reporter_cutoff: float
) -> list[Members] | None:
    """The six sections' members, in `_STATS_KINDS` order, from one round trip;
    None when the user has no node."""
    names = [name for name, _, _ in _STATS_KINDS]
    blocks = "".join(
        _members_blocks(
            f"MATCH {_scoped_match_pattern(rel_type, direction)}", f"_{name}"
        )
        for name, rel_type, direction in _STATS_KINDS
    )
    fields = ", ".join(
        f"{field}_{name}" for name in names for field in ("ids", "no_id", "reported")
    )
    result = await session.run(
        f"MATCH (user:NostrUser {{pubkey: $pubkey}}){blocks}\nRETURN {fields}",
        pubkey=pubkey,
    )
    record = await result.single()
    if record is None:
        return None
    return await _members_from(
        observer, record, [f"_{name}" for name in names], reporter_cutoff
    )


async def _section_stats_from_scorecards(
    session: AsyncNeoSession,
    pubkey: str,
    observer: str,
    verified_cutoff_by_kind: dict[str, float],
    *,
    verified_line: float,
    reporter_cutoff: float,
) -> dict[str, ConnectionStats]:
    names = [name for name, _, _ in _STATS_KINDS]
    sections = await _members_of_every_section(
        session, pubkey, observer, reporter_cutoff
    ) or [Members([], [], {}) for _ in names]
    counted = await count_members(
        observer,
        sections,
        [verified_cutoff_by_kind[name] for name in names],
        verified_line=verified_line,
    )
    return {
        name: ConnectionStats(
            total=total, verified=verified, tier_counts=ConnectionTierCounts(**tiers)
        )
        for name, (total, verified, tiers) in zip(names, counted)
    }


async def _graph_data_from_scorecards(
    session: AsyncNeoSession,
    pubkey: str,
    observer: str,
    *,
    reporter_cutoff: float,
) -> UserGraphData:
    """Unpaginated, so every member's pubkey and score is read."""
    names = [name for name, _, _ in _STATS_KINDS]
    sections = await _members_of_every_section(
        session, pubkey, observer, reporter_cutoff
    )
    if sections is None:
        return UserGraphData(influence=None, **{name: [] for name in names})
    scored = await scores_by_id(observer, {uid for m in sections for uid in m.ids})
    own = await scores_by_pubkey(observer, [pubkey])

    def rows(members: Members) -> list[UserConnection]:
        known = [
            UserConnection(
                pubkey=scored[uid][0],
                influence=scored[uid][1].influence,
                trusted_reporters=members.live.get(uid, 0),
            )
            for uid in members.ids
        ]
        unknown = [
            UserConnection(
                pubkey=pk, influence=None, trusted_reporters=members.live.get(pk, 0)
            )
            for pk in members.no_id
        ]
        return known + unknown

    return UserGraphData(
        influence=own[pubkey].influence if pubkey in own else None,
        **{name: rows(members) for name, members in zip(names, sections)},
    )


async def get_user_graph_data(
    session: AsyncNeoSession,
    pubkey: str,
    influence_key: str,
    *,
    reporter_cutoff: float,
) -> UserGraphData:
    """Single Cypher returning all 6 relationship lists (full, unpaginated) plus
    the user's own influence — used by /self and /user/{pubkey}. Row
    `trusted_reporters` is the live count above `reporter_cutoff`."""
    if settings.scores_in_postgres:
        return await _graph_data_from_scorecards(
            session,
            pubkey,
            _observer_of(influence_key),
            reporter_cutoff=reporter_cutoff,
        )
    row = _row_map()
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})

    CALL (user) {{
        MATCH (other:NostrUser)-[:FOLLOWS]->(user)
        {_bind_score()}
        RETURN collect({row}) AS followed_by
    }}

    CALL (user) {{
        MATCH (user)-[:FOLLOWS]->(other:NostrUser)
        {_bind_score()}
        RETURN collect({row}) AS following
    }}

    CALL (user) {{
        MATCH (other:NostrUser)-[:MUTES]->(user)
        {_bind_score()}
        RETURN collect({row}) AS muted_by
    }}

    CALL (user) {{
        MATCH (user)-[:MUTES]->(other:NostrUser)
        {_bind_score()}
        RETURN collect({row}) AS muting
    }}

    CALL (user) {{
        MATCH (other:NostrUser)-[:REPORTS]->(user)
        {_bind_score()}
        RETURN collect({row}) AS reported_by
    }}

    CALL (user) {{
        MATCH (user)-[:REPORTS]->(other:NostrUser)
        {_bind_score()}
        RETURN collect({row}) AS reporting
    }}

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
        reporter_cutoff=reporter_cutoff,
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
    session: AsyncNeoSession,
    pubkeys: list[str],
    observer_pubkey: str,
) -> dict[str, float | None]:
    """Return {pubkey -> influence_<observer_pubkey>} for every pubkey in
    `pubkeys`. Unknown pubkeys and pubkeys with no influence property for the
    given observer are mapped to None. Used by ORE-03 (POST /rank/pubkeys).
    """
    if not pubkeys:
        return {}

    if settings.scores_in_postgres:
        scored = await scores_by_pubkey(observer_pubkey, pubkeys)
        return {pk: scored[pk].influence if pk in scored else None for pk in pubkeys}

    influence_key = f"influence_{observer_pubkey}"
    query = """
    UNWIND $pubkeys AS pk
    OPTIONAL MATCH (user:NostrUser {pubkey: pk})
    RETURN pk AS pubkey, user[$influence_key] AS influence
    """
    result = await session.run(query, pubkeys=pubkeys, influence_key=influence_key)
    out: dict[str, float | None] = {pk: None for pk in pubkeys}
    async for record in result:
        out[record["pubkey"]] = record["influence"]
    return out


async def get_ta_observees_for_observer(
    session: AsyncNeoSession, observer_pubkey: str, cutoff: float
) -> set[str]:
    """Observees the publisher would assert a TA for: stored influence at or above
    the publish cutoff after the same 2-decimal rounding. A full label scan — the
    per-observer property can't be indexed."""
    query = """
    MATCH (user:NostrUser)
    WITH user, user[$influence_key] AS score
    WHERE score >= $floor
    RETURN user.pubkey AS pubkey, score AS influence
    """
    result = await session.run(
        query, influence_key=f"influence_{observer_pubkey}", floor=cutoff - 0.01
    )
    return {
        record["pubkey"]
        async for record in result
        if round(record["influence"], 2) >= cutoff
    }


async def get_top_inbound_by_influence(
    session: AsyncNeoSession,
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

    if settings.scores_in_postgres:
        result = await session.run(
            f"""
            MATCH (other:NostrUser)-[:{relation}]->(user:NostrUser {{pubkey: $pubkey}})
            RETURN collect(other.user_id) AS ids, collect({_NO_ID_PUBKEY}) AS no_id
            """,
            pubkey=pubkey,
        )
        record = await result.single()
        rows, _ = await page_of_members(
            observer_pubkey,
            Members(record["ids"], record["no_id"], {})
            if record
            else Members([], [], {}),
            verified_line=0.0,
            order="desc",
            limit=int(limit),
        )
        return [UserConnection(pubkey=r.pubkey, influence=r.influence) for r in rows]

    influence_key = f"influence_{observer_pubkey}"
    query = f"""
    MATCH (other:NostrUser)-[:{relation}]->(user:NostrUser {{pubkey: $pubkey}})
    WITH other, other[$influence_key] AS score
    WITH other, score, coalesce(score, -1.0) AS sort_inf
    RETURN other.pubkey AS pubkey, score AS influence
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


SHORTEST_PATHS_TIMEOUT_SECONDS = 5.0


class ShortestPathTimeout(Exception):
    """Neo4j killed the shortest-paths query at SHORTEST_PATHS_TIMEOUT_SECONDS."""


def _guard_max_hops(max_hops: int) -> None:
    # Interpolated into the pattern (Cypher can't parametrize the bound).
    if type(max_hops) is not int or not 1 <= max_hops <= 50:
        raise ValueError(f"max_hops must be an int in [1, 50], got {max_hops!r}")


async def _shortest_path_records(
    session: AsyncNeoSession, query: str, from_pubkey: str, to_pubkey: str
) -> list[Any]:
    try:
        result = await session.run(
            Query(query, timeout=SHORTEST_PATHS_TIMEOUT_SECONDS),
            from_pubkey=from_pubkey,
            to_pubkey=to_pubkey,
        )
        return [record async for record in result]
    except ClientError as e:
        if "TransactionTimedOut" in (e.code or ""):
            raise ShortestPathTimeout from e
        raise


async def get_all_shortest_follow_paths(
    session: AsyncNeoSession,
    from_pubkey: str,
    to_pubkey: str,
    max_hops: int,
) -> list[list[str]]:
    """Every shortest FOLLOWS Path as a list of pubkeys, ends included; [] when
    unreachable. Uncapped (ADR 0004); raises ShortestPathTimeout, never a
    partial set. Callers must not pass from == to.
    """
    _guard_max_hops(max_hops)
    query = f"""
    MATCH (a:NostrUser {{pubkey: $from_pubkey}}), (b:NostrUser {{pubkey: $to_pubkey}})
    MATCH p = allShortestPaths((a)-[:FOLLOWS*..{max_hops}]->(b))
    RETURN [n IN nodes(p) | n.pubkey] AS path
    """
    records = await _shortest_path_records(session, query, from_pubkey, to_pubkey)
    return [record["path"] for record in records]


async def get_shortest_follow_hops(
    session: AsyncNeoSession,
    from_pubkey: str,
    to_pubkey: str,
    max_hops: int,
) -> int | None:
    """Hops along one shortest FOLLOWS path, or None when unreachable. Stops at
    the first path found. Callers must not pass from == to.
    """
    _guard_max_hops(max_hops)
    query = f"""
    MATCH (a:NostrUser {{pubkey: $from_pubkey}}), (b:NostrUser {{pubkey: $to_pubkey}})
    MATCH p = shortestPath((a)-[:FOLLOWS*..{max_hops}]->(b))
    RETURN length(p) AS hops
    """
    records = await _shortest_path_records(session, query, from_pubkey, to_pubkey)
    return records[0]["hops"] if records else None


# ------------------------------ network alerts ------------------------------
#
# A network alert is a pubkey carrying more verified reports than its reach
# justifies. The bar scales with audience size so a large account isn't flagged
# by the same absolute report count as a small one:
#
#     N     = 4 + floor(verified_follower_count / 500)
#     alert = verified_reporter_count > N
#
# The work is split across three bounded queries rather than one big one,
# because the only expensive input is the verified follower count:
#
#   1. get_network_alert_candidates    — live reporter count, follower count
#                                        read off the node
#   2. count_above_cutoff_followers_capped — counts followers, capped; used for
#                                        any candidate with no stored count
#   3. count_verified_muters           — display-only, over final rows only
#
# Steady state is (1) + (3): `write_neo4j_results.py` persists
# `trusted_followers_<observer>`, so step (1) reads the count straight off the
# node. Step (2) is the fallback for any observer whose run carrying that
# property hasn't landed yet, and goes quiet as they backfill.
#
# Reverting the writer is NOT a clean fallback: the reader prefers the stored
# value and nothing deletes it, so written candidates would freeze at their last
# run rather than return to step (2). A revert has to remove the property too.
#
# Two things keep step (1) off a full label scan:
#
#   Anchoring. Candidates are reached *through* the REPORTS edge instead of by
#   scanning :NostrUser. Only a reported pubkey can clear N >= 4, and the
#   reported set is orders of magnitude smaller than the node count. (There is
#   no index that could help instead: the influence properties are
#   per-observer, so indexing them would mean one index per observer.)
#
#   Prefiltering. The floor term is non-negative, so N >= 4 for everyone and
#   `verified_reporter_count >= 5` is a superset of every possible alert. It
#   applies before any arithmetic without dropping a qualifying row.
#
# Retraction is fully live: the reporter count is counted off live REPORTS
# edges under the observer's reporter cutoff, not read from a stored property.
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

# Floor of the scaled threshold, before any follower allowance applies:
# N = BASE_REPORTER_THRESHOLD + floor(verified_followers / 500). It lives here
# rather than in the service because the candidate prefilter below is derived
# from it, and the two have to move together — the service's follower-count cap
# is `FOLLOWERS_PER_EXTRA_REPORT * (reporters - BASE_REPORTER_THRESHOLD)`, which
# a candidate under the prefilter would turn into a zero-or-negative Cypher
# LIMIT.
BASE_REPORTER_THRESHOLD = 4

# N is at least BASE_REPORTER_THRESHOLD for everyone, so a candidate at or below
# it cannot clear the threshold whatever its follower count turns out to be.
# Dropping those here is what keeps the arithmetic off the vast majority of
# reported pubkeys.
MIN_ALERT_REPORTERS = BASE_REPORTER_THRESHOLD + 1

# Ceiling on candidates pulled back from step (1). Far above any plausible
# real count — a backstop against a pathological graph, not a paging limit.
# Callers are expected to log when it bites rather than truncate silently.
MAX_ALERT_CANDIDATES = 5000

_ALERT_CANDIDATES_QUERY = f"""
MATCH (observer:NostrUser {{pubkey: $observer_pubkey}})
CALL (observer) {{
    MATCH (:NostrUser)-[:REPORTS]->(bob:NostrUser)
    WITH DISTINCT observer, bob
    WHERE bob.pubkey <> observer.pubkey
    WITH observer, bob, {_live_reporters("bob")} AS verified_reporters
    WHERE verified_reporters >= $min_reporters
    WITH bob, verified_reporters,
         bob[$trusted_followers_key] AS stored_followers,
         bob[$influence_key] AS influence,
         bob[$hops_key] AS hops,
         EXISTS {{ (observer)-[:FOLLOWS]->(bob) }} AS is_direct
    WHERE is_direct OR (influence IS NOT NULL AND influence > $cutoff)
    RETURN bob.pubkey AS pubkey, verified_reporters, stored_followers,
           influence, hops, is_direct
}}
RETURN pubkey, verified_reporters, stored_followers, influence, hops, is_direct
LIMIT $max_candidates
"""


async def get_network_alert_candidates(
    session: AsyncNeoSession,
    observer_pubkey: str,
    influence_key: str,
    hops_key: str,
    trusted_followers_key: str,
    cutoff: float,
    *,
    reporter_cutoff: float,
    max_candidates: int = MAX_ALERT_CANDIDATES,
) -> list[dict]:
    """Pubkeys that could be network alerts, before the report threshold applies.

    Reachability is already decided here (directly followed, or above `cutoff`),
    but the threshold is NOT — it needs a verified follower count, and
    `stored_followers` comes back None when this observer has no GrapeRank run
    carrying `trusted_followers_<observer>` yet. The caller resolves those.

    Touches no follower or muter edges; the reporter count is live, over
    REPORTS edges from raters above `reporter_cutoff`.
    """
    result = await session.run(
        _ALERT_CANDIDATES_QUERY,
        observer_pubkey=observer_pubkey,
        influence_key=influence_key,
        hops_key=hops_key,
        trusted_followers_key=trusted_followers_key,
        cutoff=cutoff,
        reporter_cutoff=reporter_cutoff,
        min_reporters=MIN_ALERT_REPORTERS,
        max_candidates=int(max_candidates),
    )
    return [dict(record) async for record in result]


async def count_above_cutoff_followers_capped(
    session: AsyncNeoSession,
    pubkeys: list[str],
    influence_key: str,
    cutoff: float,
    cap: int,
) -> dict[str, int]:
    """Above-cutoff follower counts, each stopping at `cap`.

    Fallback for candidates with no stored `trusted_followers_<observer>`.

    The observer counts as a follower here, matching
    `GrapeRankAlgorithm.countTrustedRaters` and so the stored value. Excluding
    them would need the same filter in the Java, which also feeds the
    `followers` tag on every published kind-30382 — a protocol-visible change
    for a ±1 that can only move N at an exact multiple of
    FOLLOWERS_PER_EXTRA_REPORT.

    `cap` is what makes this safe to run on a huge account. A row can only clear
    the threshold when its verified follower count is below
    `FOLLOWERS_PER_EXTRA_REPORT * (verified_reporters - BASE_REPORTER_THRESHOLD)`,
    so counting past that point cannot change any decision. Pass that value as
    `cap` and read the result as: **count < cap → exact count, row passes;
    count == cap → the true count is at least `cap`, row fails.** A row that
    survives therefore always carries a real number, never a truncated one.

    Caveat worth knowing: `cap` bounds the above-cutoff followers *collected*,
    not the follower edges *scanned*. An account with many followers but few
    above the cutoff never reaches the cap and still costs a full traversal of
    its follower edges. The cap helps most where the risk is highest (accounts
    with a large trusted following) and never hurts.

    Cypher forbids a LIMIT that varies per row, so callers bucket pubkeys by
    identical `cap` and call this once per bucket.
    """
    if not pubkeys:
        return {}

    query = """
    UNWIND $pubkeys AS pk
    MATCH (bob:NostrUser {pubkey: pk})
    CALL (bob) {
        MATCH (f:NostrUser)-[:FOLLOWS]->(bob)
        WHERE f[$influence_key] > $cutoff
        WITH f
        LIMIT $cap
        RETURN count(f) AS capped_followers
    }
    RETURN pk AS pubkey, capped_followers
    """
    result = await session.run(
        query,
        pubkeys=pubkeys,
        influence_key=influence_key,
        cutoff=cutoff,
        cap=int(cap),
    )
    return {
        record["pubkey"]: int(record["capped_followers"] or 0)
        async for record in result
    }


async def count_verified_muters(
    session: AsyncNeoSession,
    pubkeys: list[str],
    influence_key: str,
    verified_threshold: float,
) -> dict[str, int]:
    """Above-threshold muter counts, keyed by pubkey.

    Display-only — GrapeRank computes `trusted_muters` but doesn't persist it to
    Neo4j, so there is nothing stored to read. Never part of the filter, so
    callers run it last, over the already-limited result rows.
    """
    if not pubkeys:
        return {}

    query = """
    UNWIND $pubkeys AS pk
    MATCH (bob:NostrUser {pubkey: pk})
    CALL (bob) {
        MATCH (m:NostrUser)-[:MUTES]->(bob)
        WHERE m[$influence_key] > $verified_threshold
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
        record["pubkey"]: int(record["verified_muters"] or 0) async for record in result
    }


async def set_observer_scores(session: AsyncNeoSession, rows: list[dict]) -> None:
    """`rows`: `{pubkey, props}`, props keyed `<field>_<observer>` (built by the caller).
    Managed transaction: retried on transient errors (ingest can deadlock on the same nodes).
    """

    async def work(tx: AsyncManagedTransaction) -> None:
        result = await tx.run(
            """
            UNWIND $rows AS row
            MATCH (n:NostrUser {pubkey: row.pubkey})
            SET n += row.props
            """,
            rows=rows,
        )
        await result.consume()

    await session.execute_write(work)


async def get_pubkeys_scored_by_observer(
    session: AsyncNeoSession, keys: list[str]
) -> list[str]:
    """Every node holding any of the observer's score properties. A label scan."""
    result = await session.run(
        "MATCH (n:NostrUser) WHERE any(k IN $keys WHERE n[k] IS NOT NULL) "
        "RETURN n.pubkey AS pubkey",
        keys=keys,
    )
    return [r["pubkey"] async for r in result]


# The per-observer score properties the results writer stores (`PERSISTED_FIELDS`).
SCORE_PROPERTY_PREFIXES = ("influence_", "hops_", "trusted_followers_")


async def get_score_keys_on_node(session: AsyncNeoSession, pubkey: str) -> list[str]:
    """Every observer's score property names one node holds. Read by keys, so
    no label scan."""
    result = await session.run(
        "MATCH (n:NostrUser {pubkey: $pubkey}) RETURN keys(n) AS keys", pubkey=pubkey
    )
    record = await result.single()
    if record is None:
        return []
    return [k for k in record["keys"] if k.startswith(SCORE_PROPERTY_PREFIXES)]


# The edges a user's own events write: kind 3, 10000, 1984 and 10040.
OUTGOING_RATING_RELATIONSHIPS = ("FOLLOWS", "MUTES", "REPORTS", "DESIGNATES")


async def delete_outgoing_ratings(
    session: AsyncNeoSession, pubkey: str
) -> dict[str, list[str]]:
    """Delete the user's outgoing FOLLOWS / MUTES / REPORTS / DESIGNATES edges;
    the targets, by relationship type, so the Redis copies can follow."""

    async def work(tx: AsyncManagedTransaction) -> dict[str, list[str]]:
        result = await tx.run(
            """
            MATCH (:NostrUser {pubkey: $pubkey})-[r:FOLLOWS|MUTES|REPORTS|DESIGNATES]->(t:NostrUser)
            WITH r, type(r) AS kind, t.pubkey AS target
            DELETE r
            RETURN kind, collect(target) AS targets
            """,
            pubkey=pubkey,
        )
        return {record["kind"]: list(record["targets"]) async for record in result}

    return await session.execute_write(work)


async def clear_observer_scores(
    session: AsyncNeoSession, pubkeys: list[str], keys: list[str]
) -> None:
    """Remove the observer's score properties; the nodes and edges stay."""

    async def work(tx: AsyncManagedTransaction) -> None:
        result = await tx.run(
            """
            UNWIND $pubkeys AS pubkey
            MATCH (n:NostrUser {pubkey: pubkey})
            SET n += $nulls
            """,
            pubkeys=pubkeys,
            nulls=dict.fromkeys(keys),
        )
        await result.consume()

    await session.execute_write(work)


async def count_nodes_without_user_id(session) -> tuple[int, int]:
    """(nodes that should have a `user_id` and do not, nodes that cannot have one
    because their pubkey is not 64-char lowercase hex). Reads `user_id` on every
    node, `pubkey` only on those without one."""
    result = await session.run(
        """
        MATCH (u:NostrUser) WHERE u.user_id IS NULL
        WITH u.pubkey IS NOT NULL AND u.pubkey =~ '[0-9a-f]{64}' AS well_formed
        RETURN sum(CASE WHEN well_formed THEN 1 ELSE 0 END) AS missing,
               sum(CASE WHEN well_formed THEN 0 ELSE 1 END) AS malformed
        """
    )
    record = await result.single()
    return record["missing"], record["malformed"]
