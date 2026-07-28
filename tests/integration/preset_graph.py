"""Shared harness for the preset-driven read-endpoint integration tests.

Each test module brings its own node/edge fixture — the numbers are tuned per
module — but seeding, teardown and the app client are the same everywhere.

Issue: .scratch/preset-verified-counts/issues/03-preset-drive-overview-connections.md
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

import app.services.user_service as user_service_module
from app.api import app
from app.core.config import settings
from app.core.redis_db import get_redis_client
from app.routers.user.dependencies import get_verified_cutoffs
from app.services.verified_cutoffs import VerifiedCutoffs
from app.utils.observer import default_observer_pubkey
from tests.test_verified_cutoffs import SEED


def seeded_cutoffs(preset: str) -> VerifiedCutoffs:
    row = SEED[preset]
    return VerifiedCutoffs(
        follower=row["verified_followers_influence_cutoff"],
        muter=row["verified_muters_influence_cutoff"],
        reporter=row["verified_reporters_influence_cutoff"],
    )


# The real factory presets, so the fixture influences stay meaningful.
DEFAULT_CUTOFFS = seeded_cutoffs("DEFAULT")  # follower 0.02, muter 0.01, reporter 0.1
RESTRICTIVE_CUTOFFS = seeded_cutoffs("RESTRICTIVE")  # all 0.5


def fresh_driver():
    return AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )


def seed_graph(
    nodes: dict[str, tuple[float | None, int]],
    edges: list[tuple[str, str, str]],
):
    """Generator body for a `graph` fixture: seed, yield {name: hex_pubkey}, clean up.

    `nodes` maps a fixture name to (influence, trusted_reporters); an influence
    of None leaves the property absent, which is a distinct case from 0.
    """
    observer = default_observer_pubkey()
    influence_key = f"influence_{observer}"
    trusted_reporters_key = f"trusted_reporters_{observer}"
    pks = {name: Keys.generate().public_key().to_hex() for name in nodes}

    async def _seed() -> None:
        driver = fresh_driver()
        try:
            async with driver.session() as session:
                for name, (influence, trusted_reporters) in nodes.items():
                    await session.run(
                        f"MERGE (u:NostrUser {{pubkey: $pk}}) "
                        f"SET u.`{trusted_reporters_key}` = $tr "
                        + (
                            f"SET u.`{influence_key}` = $inf"
                            if influence is not None
                            else ""
                        ),
                        pk=pks[name],
                        inf=influence,
                        tr=trusted_reporters,
                    )
                for src, rel, dst in edges:
                    await session.run(
                        f"MATCH (a:NostrUser {{pubkey: $src}}), "
                        f"(b:NostrUser {{pubkey: $dst}}) MERGE (a)-[:{rel}]->(b)",
                        src=pks[src],
                        dst=pks[dst],
                    )
        finally:
            await driver.close()

    async def _teardown() -> None:
        driver = fresh_driver()
        try:
            async with driver.session() as session:
                await session.run(
                    "MATCH (u:NostrUser) WHERE u.pubkey IN $pubkeys DETACH DELETE u",
                    pubkeys=list(pks.values()),
                )
        finally:
            await driver.close()

    asyncio.run(_seed())
    try:
        yield pks
    finally:
        asyncio.run(_teardown())


@asynccontextmanager
async def api(cutoffs: VerifiedCutoffs):
    """HTTP client over the app with a loop-local Neo4j driver and Redis client.

    Same cross-loop caveat as ``test_shortest_path_integration`` — each test
    body runs in ONE ``asyncio.run`` loop, and both the module-level Neo4j
    driver and the module-level Redis client (which /overview SCARDs for its
    inbound counts) pin their connections to the loop that first used them, so
    each gets a fresh instance for the duration. `get_verified_cutoffs` is
    overridden so the observer's "saved preset" is whatever the test says it
    is, with no Postgres round-trip.
    """
    driver = fresh_driver()
    redis = get_redis_client()
    original_driver = user_service_module.neo4j_driver
    original_redis = user_service_module.redis_client
    user_service_module.neo4j_driver = driver
    user_service_module.redis_client = redis
    app.dependency_overrides[get_verified_cutoffs] = lambda: cutoffs
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_verified_cutoffs, None)
        user_service_module.neo4j_driver = original_driver
        user_service_module.redis_client = original_redis
        await redis.aclose()
        await driver.close()


async def _read(client, pubkey: str, path: str, params: dict) -> dict:
    resp = await client.get(f"/user/{pubkey}/{path}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def fetch_stats(client, pubkey: str, **params) -> dict:
    return await _read(client, pubkey, "stats", params)


async def fetch_overview(client, pubkey: str, **params) -> dict:
    return await _read(client, pubkey, "overview", params)


async def fetch_connections(client, pubkey: str, kind: str, **params) -> dict:
    return await _read(client, pubkey, "connections", {"kind": kind, **params})
