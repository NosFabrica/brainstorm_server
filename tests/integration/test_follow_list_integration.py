"""Integration test for the onboarding follow-list write.

Requires the real local stack (Neo4j + Redis), e.g. ``docker compose up -d``.
Run explicitly with::

    poetry run pytest tests/integration -m integration

It exercises ``ingest_follow_list`` against the real graph (no HTTP/auth layer),
asserting the synchronous side-effects the endpoint promises: ``FOLLOWS`` edges
in Neo4j and the ``followed_by:`` reverse-sets in Redis. The full end-to-end
"followList then graperank reflects the follows" check is a manual step (see the
plan's Verification section) — it needs the whole worker pipeline running.
"""

import asyncio

import pytest
from nostr_sdk import Keys

from app.core.redis_db import redis_client
from app.message_queue_tasks.process_strfry_event import FOLLOWED_BY_KEY_PREFIX
from app.neo4j_db.driver import driver as neo4j_driver
from app.services.onboarding_service import ingest_follow_list
from tests.conftest import signed_kind3

pytestmark = pytest.mark.integration


async def _followees_in_neo4j(caller_pubkey: str) -> set[str]:
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (:NostrUser {pubkey: $caller})-[:FOLLOWS]->(f:NostrUser) "
            "RETURN collect(f.pubkey) AS followees",
            caller=caller_pubkey,
        )
        record = await result.single()
        return set(record["followees"] if record else [])


async def _reverse_set_members(followee_pubkey: str) -> set[str]:
    members = await redis_client.smembers(f"{FOLLOWED_BY_KEY_PREFIX}{followee_pubkey}")
    return {m.decode() if isinstance(m, (bytes, bytearray)) else m for m in members}


async def _cleanup(pubkeys: list[str]) -> None:
    async with neo4j_driver.session() as session:
        await session.run(
            "MATCH (u:NostrUser) WHERE u.pubkey IN $pubkeys DETACH DELETE u",
            pubkeys=pubkeys,
        )
    for pk in pubkeys:
        await redis_client.delete(f"{FOLLOWED_BY_KEY_PREFIX}{pk}")


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
