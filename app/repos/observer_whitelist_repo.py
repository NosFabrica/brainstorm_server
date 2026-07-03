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
