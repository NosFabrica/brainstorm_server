"""Scorecards in the Scorecard database: one row per (Observer, Observee).

Callers pass pubkeys; the statements translate them through `nostr_user`. Every
function takes the caller's session, so a run's writes commit together.
"""
from collections.abc import Iterable
from itertools import islice
from typing import NamedTuple

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, INTEGER, TEXT
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.scorecard_database import scorecard_session
from app.core.tier_thresholds import (
    FLAGGED_TIER,
    TIER_HIGH,
    TIER_MEDIUM,
    TIER_MEDIUM_HIGH,
    TIER_NAMES,
)

# (observee pubkey, influence, hops, verified_followers)
ScoreRow = tuple[str, float, int, int]

_COPY_BATCH = 50_000


class MissingUserIds(RuntimeError):
    """A run named a pubkey that has no row in `nostr_user`."""


async def _copy(db: AsyncDBSession, table: str, columns: list[str], records) -> None:
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    it = iter(records)
    # In batches, so the event loop is released between them.
    while batch := list(islice(it, _COPY_BATCH)):
        await raw.driver_connection.copy_records_to_table(  # type: ignore[union-attr]
            table, records=batch, columns=columns
        )


async def observer_id_on_db(db: AsyncDBSession, observer: str) -> int:
    row = (
        await db.execute(
            text("SELECT id FROM nostr_user WHERE pubkey = :pubkey"),
            {"pubkey": observer},
        )
    ).first()
    if row is None:
        raise MissingUserIds(f"observer {observer} has no user id")
    return row[0]


async def set_observer_scores_on_db(
    db: AsyncDBSession, observer_id: int, rows: Iterable[ScoreRow]
) -> int:
    """Upsert the Observer's scores; returns how many rows changed. A pubkey
    without a user id raises `MissingUserIds` and nothing is written."""
    await db.execute(
        text(
            "CREATE TEMP TABLE score_in (pubkey text, influence double precision, hops smallint,"
            " verified_followers integer) ON COMMIT DROP"
        )
    )
    await _copy(
        db, "score_in", ["pubkey", "influence", "hops", "verified_followers"], rows
    )
    missing = (
        await db.execute(
            text(
                """
                SELECT count(*), (array_agg(i.pubkey))[1:5]
                FROM score_in i
                WHERE NOT EXISTS (SELECT 1 FROM nostr_user u WHERE u.pubkey = i.pubkey)
                """
            )
        )
    ).one()
    if missing[0]:
        raise MissingUserIds(
            f"{missing[0]} scored pubkeys have no user id: {missing[1]}"
        )
    # Unchanged rows are skipped, so they leave no dead tuple behind.
    changed = await db.execute(
        text(
            """
            WITH written AS (
                INSERT INTO scorecard AS s
                    (observer_id, observee_id, influence, hops, verified_followers)
                SELECT :observer, u.id, i.influence, i.hops, i.verified_followers
                FROM score_in i JOIN nostr_user u USING (pubkey)
                ORDER BY u.id
                ON CONFLICT (observer_id, observee_id) DO UPDATE
                SET influence = EXCLUDED.influence,
                    hops = EXCLUDED.hops,
                    verified_followers = EXCLUDED.verified_followers
                WHERE (s.influence, s.hops, s.verified_followers) IS DISTINCT FROM
                    (EXCLUDED.influence, EXCLUDED.hops, EXCLUDED.verified_followers)
                RETURNING 1
            )
            SELECT count(*) FROM written
            """
        ),
        {"observer": observer_id},
    )
    return changed.scalar_one()


async def clear_observer_scores_on_db(
    db: AsyncDBSession, observer_id: int, pubkeys: Iterable[str]
) -> int:
    """Delete the Observer's scores for `pubkeys`; a pubkey with no id has none."""
    wanted = list(pubkeys)
    if not wanted:
        return 0
    deleted = await db.execute(
        text(
            """
            WITH gone AS (
                DELETE FROM scorecard s USING nostr_user u
                WHERE s.observer_id = :observer AND u.id = s.observee_id
                    AND u.pubkey = ANY(:pubkeys)
                RETURNING 1
            )
            SELECT count(*) FROM gone
            """
        ).bindparams(bindparam("pubkeys", type_=ARRAY(TEXT))),
        {"observer": observer_id, "pubkeys": wanted},
    )
    return deleted.scalar_one()


async def clear_unreached_scores_on_db(
    db: AsyncDBSession, observer_id: int, reached: Iterable[str]
) -> int:
    """Delete the Observer's scores for everyone not in `reached`."""
    await db.execute(text("CREATE TEMP TABLE reached_in (pubkey text) ON COMMIT DROP"))
    await _copy(db, "reached_in", ["pubkey"], ((pubkey,) for pubkey in reached))
    deleted = await db.execute(
        text(
            """
            WITH gone AS (
                DELETE FROM scorecard s
                WHERE s.observer_id = :observer AND NOT EXISTS (
                    SELECT 1 FROM reached_in r JOIN nostr_user u USING (pubkey)
                    WHERE u.id = s.observee_id
                )
                RETURNING 1
            )
            SELECT count(*) FROM gone
            """
        ),
        {"observer": observer_id},
    )
    return deleted.scalar_one()


async def get_pubkeys_scored_by_observer_on_db(
    db: AsyncDBSession, observer_id: int
) -> list[str]:
    result = await db.execute(
        text(
            "SELECT u.pubkey FROM scorecard s JOIN nostr_user u ON u.id = s.observee_id"
            " WHERE s.observer_id = :observer"
        ),
        {"observer": observer_id},
    )
    return [row[0] for row in result]


async def clear_scores_about_on_db(db: AsyncDBSession, pubkey: str) -> int:
    """Delete every Observer's score for one Observee. Scans the table: there is
    no index by Observee, and this is a rare admin action."""
    deleted = await db.execute(
        text(
            """
            WITH gone AS (
                DELETE FROM scorecard
                WHERE observee_id = (SELECT id FROM nostr_user WHERE pubkey = :pubkey)
                RETURNING 1
            )
            SELECT count(*) FROM gone
            """
        ),
        {"pubkey": pubkey},
    )
    return deleted.scalar_one()


# ----------------- reads -----------------


class Score(NamedTuple):
    """What one Observer stores about one Observee; all None when nothing is stored."""

    influence: float | None
    hops: int | None
    verified_followers: int | None


NO_SCORE = Score(None, None, None)

_OBSERVER_ID = "(SELECT id FROM nostr_user WHERE pubkey = :observer)"


async def scores_by_pubkey(observer: str, pubkeys: Iterable[str]) -> dict[str, Score]:
    """The Observer's stored scores for `pubkeys`. A pubkey with none is left out."""
    wanted = list(dict.fromkeys(pubkeys))
    if not wanted:
        return {}
    async with scorecard_session() as db:
        rows = await db.execute(
            text(
                f"""
                SELECT u.pubkey, s.influence, s.hops, s.verified_followers
                FROM nostr_user u
                JOIN scorecard s ON s.observee_id = u.id AND s.observer_id = {_OBSERVER_ID}
                WHERE u.pubkey = ANY(:pubkeys)
                """
            ).bindparams(bindparam("pubkeys", type_=ARRAY(TEXT))),
            {"observer": observer, "pubkeys": wanted},
        )
        return {row[0]: Score(row[1], row[2], row[3]) for row in rows}


async def scores_by_id(
    observer: str, user_ids: Iterable[int]
) -> dict[int, tuple[str, Score]]:
    """Pubkey and the Observer's stored score for each User id the graph returned.
    An id the table does not know raises: the two stores disagree."""
    wanted = list(dict.fromkeys(user_ids))
    if not wanted:
        return {}
    async with scorecard_session() as db:
        rows = await db.execute(
            text(
                f"""
                SELECT u.id, u.pubkey, s.influence, s.hops, s.verified_followers
                FROM nostr_user u
                LEFT JOIN scorecard s
                    ON s.observee_id = u.id AND s.observer_id = {_OBSERVER_ID}
                WHERE u.id = ANY(:ids)
                """
            ).bindparams(bindparam("ids", type_=ARRAY(INTEGER))),
            {"observer": observer, "ids": wanted},
        )
        found = {row[0]: (row[1], Score(row[2], row[3], row[4])) for row in rows}
    missing = [uid for uid in wanted if uid not in found]
    if missing:
        raise MissingUserIds(
            f"{len(missing)} user ids from the graph are not in nostr_user: {missing[:5]}"
        )
    return found


# ----------------- sections: tier, sort and page in SQL -----------------


class Members(NamedTuple):
    """Who the graph put in a section. `live` is the live reporter count of the
    few members anyone reports, keyed by User id, or by pubkey for a node that
    has no id."""

    ids: list[int]
    no_id: list[str]
    live: dict[int | str, int]


class MemberRow(NamedTuple):
    pubkey: str
    influence: float | None
    reporters: int
    tier: str
    sort_inf: float


# `tier_thresholds.classify_tier` in SQL; the equivalence tests hold the two together.
_TIER_SQL = f"""
    CASE WHEN influence > :verified_line THEN
        CASE WHEN influence >= {TIER_HIGH} THEN 'high'
             WHEN influence >= {TIER_MEDIUM_HIGH} THEN 'medium_high'
             WHEN influence >= {TIER_MEDIUM} THEN 'medium'
             ELSE 'medium_low' END
    WHEN reporters >= 2 THEN '{FLAGGED_TIER}'
    ELSE 'low' END
"""

# Every member with its pubkey, Influence, live reporter count and tier. A
# member without a User id has no row anywhere: it rides in by pubkey, unscored.
_MEMBERS_SQL = f"""
    live AS (
        SELECT * FROM unnest(CAST(:live_ids AS integer[]), CAST(:live_counts AS integer[]))
            AS t(id, reporters)
    ), known AS (
        SELECT i.section, u.pubkey, s.influence, coalesce(l.reporters, 0) AS reporters
        FROM unnest(CAST(:sections AS integer[]), CAST(:ids AS integer[])) AS i(section, id)
        JOIN nostr_user u ON u.id = i.id
        LEFT JOIN scorecard s
            ON s.observee_id = u.id AND s.observer_id = {_OBSERVER_ID}
        LEFT JOIN live l ON l.id = i.id
    ), member AS (
        SELECT * FROM known
        UNION ALL
        SELECT section, pubkey, NULL::double precision, reporters
        FROM unnest(
            CAST(:no_id_sections AS integer[]),
            CAST(:no_id AS text[]),
            CAST(:no_id_counts AS integer[])
        ) AS t(section, pubkey, reporters)
    ), tiered AS (
        SELECT *, {_TIER_SQL} AS tier, coalesce(influence, -1.0) AS sort_inf FROM member
    )
"""


def _member_params(observer: str, sections: list[Members]) -> dict:
    # A member's count is the same in every section it is in: listed once.
    seen = {
        key: count
        for members in sections
        for key, count in members.live.items()
        if isinstance(key, int)
    }
    no_id_counts = [
        members.live.get(pk, 0) for members in sections for pk in members.no_id
    ]
    return {
        "observer": observer,
        "sections": [n for n, m in enumerate(sections) for _ in m.ids],
        "ids": [uid for m in sections for uid in m.ids],
        "live_ids": list(seen),
        "live_counts": list(seen.values()),
        "no_id_sections": [n for n, m in enumerate(sections) for _ in m.no_id],
        "no_id": [pk for m in sections for pk in m.no_id],
        "no_id_counts": no_id_counts,
    }


def _check_joined(joined: int, sections: list[Members]) -> None:
    wanted = sum(len(m.ids) for m in sections)
    if joined != wanted:
        raise MissingUserIds(
            f"{wanted - joined} user ids from the graph are not in nostr_user"
        )


async def page_of_members(
    observer: str,
    members: Members,
    *,
    verified_line: float,
    order: str,
    limit: int,
    cursor: tuple[float, str] | None = None,
    tier: str | None = None,
    verified_cutoff: float | None = None,
) -> tuple[list[MemberRow], int]:
    """One page of a section, ordered by Influence (`order`; none sorts as -1)
    then pubkey, and the total the filter matches whatever the cursor."""
    descending = order != "asc"
    filters = ["true"]
    params = _member_params(observer, [members])
    params.update(verified_line=verified_line, limit=limit)
    if tier in TIER_NAMES:
        filters.append("tier = :tier")
        params["tier"] = tier
    if verified_cutoff is not None:
        filters.append("influence > :verified_cutoff")
        params["verified_cutoff"] = verified_cutoff
    after = "true"
    if cursor is not None:
        after = (
            f"(sort_inf {'<' if descending else '>'} :cursor_inf"
            " OR (sort_inf = :cursor_inf AND pubkey > :cursor_pk))"
        )
        params.update(cursor_inf=cursor[0], cursor_pk=cursor[1])
    direction = "DESC" if descending else "ASC"
    async with scorecard_session() as db:
        result = await db.execute(
            text(
                f"""
                WITH {_MEMBERS_SQL}, matching AS (
                    SELECT * FROM tiered WHERE {" AND ".join(filters)}
                ), page AS (
                    SELECT * FROM matching WHERE {after}
                    ORDER BY sort_inf {direction}, pubkey ASC
                    LIMIT :limit
                )
                SELECT c.joined, c.total, p.pubkey, p.influence, p.reporters, p.tier,
                    p.sort_inf
                FROM (
                    SELECT (SELECT count(*) FROM known) AS joined,
                        (SELECT count(*) FROM matching) AS total
                ) c
                LEFT JOIN page p ON true
                ORDER BY p.sort_inf {direction}, p.pubkey ASC
                """
            ),
            params,
        )
        rows = result.all()
    _check_joined(rows[0][0], [members])
    return (
        [MemberRow(*row[2:]) for row in rows if row[2] is not None],
        rows[0][1],
    )


async def count_members(
    observer: str,
    sections: list[Members],
    verified_cutoffs: list[float],
    *,
    verified_line: float,
) -> list[tuple[int, int, dict[str, int]]]:
    """Per section: total, verified against that section's cutoff, and the tier counts."""
    params = _member_params(observer, sections)
    params.update(verified_line=verified_line, cutoffs=verified_cutoffs)
    tier_counts = ", ".join(
        f"count(*) FILTER (WHERE t.tier = '{tier}')" for tier in TIER_NAMES
    )
    async with scorecard_session() as db:
        result = await db.execute(
            text(
                f"""
                WITH {_MEMBERS_SQL}
                SELECT t.section, (SELECT count(*) FROM known) AS joined, count(*),
                    count(*) FILTER (WHERE t.influence > c.cutoff), {tier_counts}
                FROM tiered t
                JOIN unnest(CAST(:cutoffs AS double precision[])) WITH ORDINALITY
                    AS c(cutoff, n) ON c.n = t.section + 1
                GROUP BY t.section
                """
            ),
            params,
        )
        rows = {row[0]: row for row in result}
    if rows:
        _check_joined(next(iter(rows.values()))[1], sections)
    elif any(m.ids for m in sections):
        _check_joined(0, sections)
    out = []
    for n in range(len(sections)):
        row = rows.get(n)
        counts = dict(zip(TIER_NAMES, row[4:] if row else [0] * len(TIER_NAMES)))
        out.append((row[2] if row else 0, row[3] if row else 0, counts))
    return out
