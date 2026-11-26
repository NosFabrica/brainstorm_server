from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings

SERVER_STATEMENT_TIMEOUT_MS = 60_000
SERVER_COMMAND_TIMEOUT_SECONDS = 180.0

# Migrations: wait at most 10 s for a lock, never cap a statement.
MIGRATION_SERVER_SETTINGS = {"lock_timeout": "10s", "statement_timeout": "0"}

_engine: AsyncEngine | None = None
_unbounded = False


def unbounded_statements() -> None:
    """For scripts: no statement_timeout and no client timeout on this process's
    connections. Call before the first use of the engine."""
    global _unbounded
    if _engine is not None and not _unbounded:
        raise RuntimeError("the Scorecard engine already exists with server limits")
    _unbounded = True


def _connect_args() -> dict:
    if _unbounded:
        return {"command_timeout": None, "server_settings": {"statement_timeout": "0"}}
    return {
        "command_timeout": SERVER_COMMAND_TIMEOUT_SECONDS,
        "server_settings": {"statement_timeout": str(SERVER_STATEMENT_TIMEOUT_MS)},
    }


def scorecard_engine() -> AsyncEngine:
    """The Scorecard database's engine, created on first use."""
    global _engine
    if _engine is None:
        if not settings.scorecard_db_url:
            raise RuntimeError("SCORECARD_DB_URL is not set")
        _engine = create_async_engine(
            settings.scorecard_db_url,
            pool_size=10,
            max_overflow=20,
            pool_timeout=60,
            pool_pre_ping=True,
            connect_args=_connect_args(),
        )
    return _engine


async def dispose_scorecard_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def scorecard_session() -> AsyncGenerator[AsyncDBSession, None]:
    """A session that commits on a clean exit and rolls back on any error."""
    factory = async_sessionmaker(bind=scorecard_engine(), expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
