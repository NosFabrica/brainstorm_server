"""Migrations for the Scorecard database: `alembic -n scorecard upgrade head`."""
import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import context
from app.core.config import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

VERSION_TABLE = "alembic_version_scorecard"


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection, target_metadata=None, version_table=VERSION_TABLE
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    if not settings.scorecard_db_url:
        raise RuntimeError("SCORECARD_DB_URL is not set")
    engine = create_async_engine(settings.scorecard_db_url, poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("the Scorecard migrations run online only")
asyncio.run(run_migrations_online())
