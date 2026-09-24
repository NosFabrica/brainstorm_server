from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import ShortUrl


async def select_short_url_by_code_on_db(
    db: AsyncDBSession, short_code: str
) -> ShortUrl | None:
    """Resolve a code. ``short_code`` must already be normalized by the caller."""
    result = await db.execute(select(ShortUrl).where(ShortUrl.short_code == short_code))
    return result.scalar_one_or_none()


async def select_short_url_by_content_on_db(
    db: AsyncDBSession, pubkey: str, relays_fingerprint: str
) -> ShortUrl | None:
    """The dedup lookup: has this (pubkey, relay-set) already been minted?"""
    result = await db.execute(
        select(ShortUrl).where(
            ShortUrl.pubkey == pubkey,
            ShortUrl.relays_fingerprint == relays_fingerprint,
        )
    )
    return result.scalar_one_or_none()


async def insert_short_url_on_db(
    db: AsyncDBSession,
    short_code: str,
    pubkey: str,
    relays_fingerprint: str,
    relays: list[str],
) -> ShortUrl:
    """Insert a row and flush so a unique violation surfaces here, not at commit."""
    row = ShortUrl(
        short_code=short_code,
        pubkey=pubkey,
        relays_fingerprint=relays_fingerprint,
        relays=relays,
    )
    db.add(row)
    await db.flush()
    return row
