"""Shared harness for the tagging/Trusted-List integration tests.

Each test runs in ONE `asyncio.run` with its OWN engine. The module-level
`app.core.database.engine` cannot be reused across tests here: asyncpg binds its
pool to the loop that created it, so a second `asyncio.run` inherits a pool
whose connections belong to a closed loop ("Event loop is closed"). A per-test
engine that is disposed inside the same loop avoids that entirely.
"""
from __future__ import annotations

import asyncio

from neo4j import AsyncGraphDatabase
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db_models import NostrTagElement, NostrUserTagging


def run_with_db(work):
    """Run `async work(session) -> T` against a fresh engine, cleaning the
    tagging tables before and after. Returns T."""

    async def _go():
        engine = create_async_engine(settings.db_url, future=True)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, future=True)
        try:
            async with factory() as db:
                await db.execute(delete(NostrUserTagging))
                await db.execute(delete(NostrTagElement))
                await db.commit()
            async with factory() as db:
                result = await work(db)
                await db.commit()
            return result
        finally:
            async with factory() as db:
                await db.execute(delete(NostrUserTagging))
                await db.execute(delete(NostrTagElement))
                await db.commit()
            await engine.dispose()

    return asyncio.run(_go())


def run_with_db_and_graph(work, nodes: dict[str, float | None], observer: str):
    """As `run_with_db`, plus a seeded Neo4j graph.

    `nodes` maps pubkey -> influence under `observer`. A value of None leaves the
    property ABSENT, which is a distinct case from 0.0 — "never scored" must not
    be read as "scored zero".
    """

    async def _go():
        engine = create_async_engine(settings.db_url, future=True)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, future=True)
        neo = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        influence_key = f"influence_{observer}"
        try:
            async with neo.session() as s:
                for pk, influence in nodes.items():
                    if influence is None:
                        await s.run("MERGE (u:NostrUser {pubkey: $pk})", pk=pk)
                    else:
                        await s.run(
                            f"MERGE (u:NostrUser {{pubkey: $pk}}) "
                            f"SET u.`{influence_key}` = $inf",
                            pk=pk,
                            inf=influence,
                        )
            async with factory() as db:
                await db.execute(delete(NostrUserTagging))
                await db.execute(delete(NostrTagElement))
                await db.commit()
            async with factory() as db:
                result = await work(db)
                await db.commit()
            return result
        finally:
            async with neo.session() as s:
                for pk in nodes:
                    await s.run(
                        "MATCH (u:NostrUser {pubkey: $pk}) DETACH DELETE u", pk=pk
                    )
            await neo.close()
            async with factory() as db:
                await db.execute(delete(NostrUserTagging))
                await db.execute(delete(NostrTagElement))
                await db.commit()
            await engine.dispose()

    return asyncio.run(_go())


def neo_driver():
    """A fresh Neo4j driver bound to whatever loop calls this."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )


async def seed_influence(nodes: dict[str, float], observer: str) -> None:
    """Set `influence_<observer>` on each pubkey. Awaited inside the caller's
    own loop so it composes with end-to-end service tests."""
    influence_key = f"influence_{observer}"
    driver = neo_driver()
    try:
        async with driver.session() as session:
            for pk, influence in nodes.items():
                await session.run(
                    f"MERGE (u:NostrUser {{pubkey: $pk}}) "
                    f"SET u.`{influence_key}` = $inf",
                    pk=pk,
                    inf=influence,
                )
    finally:
        await driver.close()
