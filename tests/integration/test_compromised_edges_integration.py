"""Marking a key compromised cuts its own ratings where GrapeRank reads them:
its outgoing FOLLOWS / MUTES / REPORTS / DESIGNATES edges in Neo4j, its entries
in the targets' `followed_by:` / `muted_by:` / `reported_by:` sets, and its
`designates:` set. Edges pointing AT the key stay: it is still scored (at 0).

Requires the real local stack (Neo4j + Redis). Run explicitly with::

    poetry run pytest tests/integration -m integration
"""

import asyncio

import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

import app.message_queue_tasks.process_strfry_event as ingest
from app.core.config import settings
from app.core.redis_db import get_redis_client
from app.services import compromised_service as svc

pytestmark = pytest.mark.integration


def _pk() -> str:
    return Keys.generate().public_key().to_hex()


def _driver():
    return AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )


def _members(raw) -> set[str]:
    return {m.decode() if isinstance(m, (bytes, bytearray)) else m for m in raw}


def test_marking_cuts_the_keys_own_ratings_and_keeps_those_about_it(monkeypatch):
    stolen, followee, muted, reported, provider, follower = (_pk() for _ in range(6))
    everyone = [stolen, followee, muted, reported, provider, follower]

    async def body():
        driver, redis = _driver(), get_redis_client()
        monkeypatch.setattr(svc, "neo4j_driver", driver)
        monkeypatch.setattr(svc, "redis_client", redis)
        monkeypatch.setattr(ingest, "redis_client", redis)
        try:
            async with driver.session() as s:
                await s.run(
                    """
                    UNWIND $pks AS pk MERGE (:NostrUser {pubkey: pk})
                    WITH 1 AS _
                    MATCH (c:NostrUser {pubkey: $stolen}), (f:NostrUser {pubkey: $followee}),
                          (m:NostrUser {pubkey: $muted}), (r:NostrUser {pubkey: $reported}),
                          (p:NostrUser {pubkey: $provider}), (x:NostrUser {pubkey: $follower})
                    MERGE (c)-[:FOLLOWS]->(f) MERGE (c)-[:MUTES]->(m)
                    MERGE (c)-[:REPORTS]->(r) MERGE (c)-[:DESIGNATES]->(p)
                    MERGE (x)-[:FOLLOWS]->(c) MERGE (x)-[:FOLLOWS]->(f)
                    """,
                    pks=everyone, stolen=stolen, followee=followee, muted=muted,
                    reported=reported, provider=provider, follower=follower,
                )
            await redis.sadd(f"followed_by:{followee}", stolen, follower)
            await redis.sadd(f"followed_by:{stolen}", follower)
            await redis.sadd(f"muted_by:{muted}", stolen)
            await redis.sadd(f"reported_by:{reported}", stolen)
            await redis.sadd(f"designates:{stolen}", provider)

            assert await svc.cut_outgoing_ratings(stolen) == 4

            async with driver.session() as s:
                result = await s.run(
                    "MATCH (a:NostrUser)-[r]->(b:NostrUser) "
                    "WHERE a.pubkey IN $pks RETURN a.pubkey AS a, type(r) AS t, b.pubkey AS b",
                    pks=everyone,
                )
                edges = {(rec["a"], rec["t"], rec["b"]) async for rec in result}
            assert edges == {
                (follower, "FOLLOWS", stolen),
                (follower, "FOLLOWS", followee),
            }
            assert _members(await redis.smembers(f"followed_by:{followee}")) == {follower}
            assert _members(await redis.smembers(f"followed_by:{stolen}")) == {follower}
            assert not await redis.exists(f"muted_by:{muted}")
            assert not await redis.exists(f"reported_by:{reported}")
            assert not await redis.exists(f"designates:{stolen}")
        finally:
            async with driver.session() as s:
                await s.run(
                    "MATCH (u:NostrUser) WHERE u.pubkey IN $pks DETACH DELETE u",
                    pks=everyone,
                )
            for prefix in ("followed_by:", "muted_by:", "reported_by:", "designates:"):
                await redis.delete(*(f"{prefix}{pk}" for pk in everyone))
            await driver.close()
            await redis.aclose()

    asyncio.run(body())
