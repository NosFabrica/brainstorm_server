"""Scorecards in the Scorecard database: one row per (Observer, Observee).

Callers pass pubkeys; the statements translate them through `nostr_user`. Every
function takes the caller's session, so a run's writes commit together.
"""
from collections.abc import Iterable
from itertools import islice

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, TEXT
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

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
            "CREATE TEMP TABLE score_in (pubkey text, influence real, hops smallint,"
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
