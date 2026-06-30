"""Integration tests for the onboarding follow-list write.

Requires the real local stack (Neo4j + Redis), e.g. ``docker compose up -d``.
Run explicitly with::

    poetry run pytest tests/integration -m integration

Two layers are covered:

* ``test_ingest_writes_follows_to_neo4j_and_redis`` calls ``ingest_follow_list``
  directly (no HTTP/auth layer).
* ``test_endpoint_persists_follows_before_responding`` drives the real
  ``POST /user/followList`` route (auth override + rate-limit + the in-request
  write), proving the side-effects land *before* the 200 returns.

Both assert the synchronous side-effects the endpoint promises: ``FOLLOWS``
edges in Neo4j and the ``followed_by:`` reverse-sets in Redis. The full
end-to-end "followList then graperank reflects the follows" check is a manual
step (see the plan's Verification section) — it needs the whole worker pipeline
running.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

from app.core.config import settings
from app.core.redis_db import get_redis_client, redis_client
from app.message_queue_tasks.process_strfry_event import FOLLOWED_BY_KEY_PREFIX
from app.neo4j_db.driver import driver as neo4j_driver
from app.services.onboarding_service import ingest_follow_list
from tests.conftest import signed_kind3

pytestmark = pytest.mark.integration


async def _followees_in_neo4j(caller_pubkey: str, neo4j=neo4j_driver) -> set[str]:
    async with neo4j.session() as session:
        result = await session.run(
            "MATCH (:NostrUser {pubkey: $caller})-[:FOLLOWS]->(f:NostrUser) "
            "RETURN collect(f.pubkey) AS followees",
            caller=caller_pubkey,
        )
        record = await result.single()
        return set(record["followees"] if record else [])


async def _reverse_set_members(followee_pubkey: str, redis=redis_client) -> set[str]:
    members = await redis.smembers(f"{FOLLOWED_BY_KEY_PREFIX}{followee_pubkey}")
    return {m.decode() if isinstance(m, (bytes, bytearray)) else m for m in members}


async def _cleanup(pubkeys: list[str], neo4j=neo4j_driver, redis=redis_client) -> None:
    async with neo4j.session() as session:
        await session.run(
            "MATCH (u:NostrUser) WHERE u.pubkey IN $pubkeys DETACH DELETE u",
            pubkeys=pubkeys,
        )
    for pk in pubkeys:
        await redis.delete(f"{FOLLOWED_BY_KEY_PREFIX}{pk}")


@asynccontextmanager
async def _fresh_clients():
    """A throwaway Neo4j driver + Redis client bound to the *current* loop.

    The HTTP test exercises the app singletons on ``TestClient``'s internal
    event loop; reusing those singletons to read back from this test's loop
    would raise cross-loop errors. Fresh clients sidestep that — the data they
    read is the same, it lives in the real databases.
    """
    neo4j = AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )
    redis = get_redis_client()
    try:
        yield neo4j, redis
    finally:
        await neo4j.close()
        await redis.aclose()


def test_ingest_writes_follows_to_neo4j_and_redis():
    caller = Keys.generate()
    caller_pubkey = caller.public_key().to_hex()
    followees = [Keys.generate().public_key().to_hex() for _ in range(3)]

    async def _run():
        try:
            count = await ingest_follow_list(
                caller_pubkey, signed_kind3(caller, followees)
            )

            assert count == 3
            assert await _followees_in_neo4j(caller_pubkey) == set(followees)
            for followee in followees:
                assert caller_pubkey in await _reverse_set_members(followee)
        finally:
            await _cleanup([caller_pubkey, *followees])

    asyncio.run(_run())


def test_endpoint_persists_follows_before_responding(client, caller, monkeypatch):
    """Drive the real ``POST /user/followList`` against live Neo4j + Redis.

    Because the write is synchronous (in-request), the ``FOLLOWS`` edges and
    ``followed_by:`` reverse-sets must already exist the instant the 200
    returns — read back immediately, with no polling.

    The endpoint is handed its *own* Neo4j driver + Redis client (in place of
    the app singletons) so they bind lazily to ``TestClient``'s portal loop.
    The app singletons may already be bound to the service-level test's
    now-closed ``asyncio.run`` loop, which would raise cross-loop errors deep in
    the drivers. These per-test clients are intentionally not closed: shutting
    them down would itself need the portal loop, and they're cheap to leak in an
    opt-in integration run.
    """
    endpoint_neo4j = AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )
    endpoint_redis = get_redis_client()
    monkeypatch.setattr("app.services.onboarding_service.neo4j_driver", endpoint_neo4j)
    monkeypatch.setattr(
        "app.message_queue_tasks.process_strfry_event.redis_client", endpoint_redis
    )

    followees = [Keys.generate().public_key().to_hex() for _ in range(3)]
    caller_pubkey = caller.pubkey
    body = {"signed_event": signed_kind3(caller.keys, followees)}

    async def _verify():
        async with _fresh_clients() as (neo4j, redis):
            assert await _followees_in_neo4j(caller_pubkey, neo4j) == set(followees)
            for followee in followees:
                assert caller_pubkey in await _reverse_set_members(followee, redis)

    async def _cleanup_fresh():
        async with _fresh_clients() as (neo4j, redis):
            await _cleanup([caller_pubkey, *followees], neo4j, redis)

    try:
        response = client.post("/user/followList", json=body)

        assert response.status_code == 200
        assert response.json()["data"]["followCount"] == 3
        asyncio.run(_verify())
    finally:
        asyncio.run(_cleanup_fresh())
