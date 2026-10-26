from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import ShortUrl
from app.utils.shorturl_entities import EntityKind


async def select_short_url_by_code_on_db(
    db: AsyncDBSession, short_code: str
) -> ShortUrl | None:
    """Resolve a code. ``short_code`` must already be normalized by the caller."""
    result = await db.execute(select(ShortUrl).where(ShortUrl.short_code == short_code))
    return result.scalar_one_or_none()


async def select_short_url_by_entity_on_db(
    db: AsyncDBSession, kind: EntityKind, entity_key: str
) -> ShortUrl | None:
    """The dedup lookup: has this entity already been minted?"""
    result = await db.execute(
        select(ShortUrl).where(
            ShortUrl.kind == kind,
            ShortUrl.entity_key == entity_key,
        )
    )
    return result.scalar_one_or_none()


async def insert_short_url_on_db(
    db: AsyncDBSession,
    short_code: str,
    kind: EntityKind,
    entity_key: str,
    relays: list[str],
) -> ShortUrl:
    """Insert a row and flush so a unique violation surfaces here, not at commit."""
    row = ShortUrl(
        short_code=short_code,
        kind=kind,
        entity_key=entity_key,
        relays=relays,
    )
    db.add(row)
    await db.flush()
    return row
