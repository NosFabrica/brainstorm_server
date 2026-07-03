from sqlalchemy import func, select
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


async def select_observer_whitelist_on_db(
    db: AsyncDBSession, observer_pubkey: str
) -> ObserverWhitelist | None:
    stmt = select(ObserverWhitelist).where(
        ObserverWhitelist.observer_pubkey == observer_pubkey
    )
    result = await db.execute(stmt)
    return result.scalars().first()
