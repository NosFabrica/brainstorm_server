from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import ObserverWhitelist


async def upsert_observer_whitelist_on_db(
    db: AsyncDBSession,
    observer_pubkey: str,
    scores: dict[str, float],
    request_id: int,
) -> None:
    stmt = insert(ObserverWhitelist).values(
        observer_pubkey=observer_pubkey,
        scores=scores,
        last_request_id=request_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ObserverWhitelist.observer_pubkey],
        set_={
            "scores": stmt.excluded.scores,
            "last_request_id": stmt.excluded.last_request_id,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)


async def select_observer_whitelist_updated_at(
    db: AsyncDBSession, observer_pubkey: str
) -> datetime | None:
    # Selecting only updated_at avoids detoasting the multi-MB scores blob —
    # cheap enough to run on every request for the ETag / 304 path.
    stmt = select(ObserverWhitelist.updated_at).where(
        ObserverWhitelist.observer_pubkey == observer_pubkey
    )
    result = await db.execute(stmt)
    return result.scalars().first()


# Filter above-threshold observees server-side so the ~99k-key scores blob is
# never parsed into a Python dict on the event loop; only matching keys return.
_WHITELISTED_PUBKEYS_SQL = text(
    """
    SELECT e.key
    FROM observerwhitelist w,
         jsonb_each_text(w.scores) AS e(key, value)
    WHERE w.observer_pubkey = :pubkey
      AND e.value::numeric >= :threshold
    """
)


async def select_whitelisted_pubkeys_of_observer(
    db: AsyncDBSession, observer_pubkey: str, threshold: float
) -> list[str]:
    result = await db.execute(
        _WHITELISTED_PUBKEYS_SQL,
        {"pubkey": observer_pubkey, "threshold": threshold},
    )
    return [row[0] for row in result]


# Same server-side filter, carrying each observee's Rank (CONTEXT.md): the
# stored influence is already rounded to 2dp, so `× 100` on numeric is exact and
# matches the `rank` tag of the published Trusted Assertion.
_WHITELISTED_RANKS_SQL = text(
    """
    SELECT e.key, (e.value::numeric * 100)::int AS rank
    FROM observerwhitelist w,
         jsonb_each_text(w.scores) AS e(key, value)
    WHERE w.observer_pubkey = :pubkey
      AND e.value::numeric * 100 >= :min_rank
    ORDER BY rank DESC, e.key
    """
)


async def select_whitelisted_ranks_of_observer(
    db: AsyncDBSession, observer_pubkey: str, min_rank: int
) -> dict[int, list[str]]:
    """Observee pubkeys bucketed by Rank, highest Rank first.

    Bucketed rather than `{pubkey: rank}`: the rank is written once per bucket
    instead of once per key, and a consumer filters by taking whole buckets.
    """
    buckets: dict[int, list[str]] = {}
    for pubkey, rank in await select_whitelisted_rank_rows_of_observer(
        db, observer_pubkey, min_rank
    ):
        buckets.setdefault(rank, []).append(pubkey)
    return buckets


async def select_whitelisted_rank_rows_of_observer(
    db: AsyncDBSession, observer_pubkey: str, min_rank: int
) -> list[tuple[str, int]]:
    """`(observee pubkey, Rank)` rows at or above `min_rank`, highest Rank first."""
    result = await db.execute(
        _WHITELISTED_RANKS_SQL,
        {"pubkey": observer_pubkey, "min_rank": min_rank},
    )
    return [(row[0], row[1]) for row in result]
