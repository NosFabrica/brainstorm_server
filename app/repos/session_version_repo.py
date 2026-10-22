"""Session versions, read through a short Redis cache over `brainstorm_nsec.session_version`."""

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.repos.brainstorm_nsec import get_session_version_on_db

logger = loggr.get_logger(__name__)

# Revocation bound: bumps write through; a racing stale or failed write lingers this long.
SESSION_VERSION_CACHE_TTL_SECONDS = 60


def _cache_key(pubkey: str) -> str:
    return f"session_version:{pubkey}"


async def cache_session_version(pubkey: str, version: int) -> None:
    try:
        await redis_client.set(
            _cache_key(pubkey), version, ex=SESSION_VERSION_CACHE_TTL_SECONDS
        )
    except RedisError:
        logger.warning("Could not cache session version for %s", pubkey)


async def read_session_version_on_db(db: AsyncDBSession, pubkey: str) -> int:
    try:
        cached = await redis_client.get(_cache_key(pubkey))
    except RedisError:
        cached = None
    if cached is not None:
        return int(cached)
    version = await get_session_version_on_db(db, pubkey)
    await cache_session_version(pubkey, version)
    return version
