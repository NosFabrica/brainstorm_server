"""Session versions: bumping a user's version revokes every session token minted before."""

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.brainstorm_nsec import bump_session_version_on_db
from app.repos.session_version_repo import (
    cache_session_version,
    read_session_version_on_db,
)


async def current_session_version(db: AsyncDBSession, pubkey: str) -> int:
    return await read_session_version_on_db(db, pubkey)


async def revoke_sessions(db: AsyncDBSession, pubkey: str) -> int | None:
    """Bump the user's version. None when the user is unknown."""
    version = await bump_session_version_on_db(db, pubkey)
    if version is None:
        return None
    # Commit first: a cached bump that rolled back would leave the cache ahead of the DB.
    await db.commit()
    await cache_session_version(pubkey, version)
    return version
