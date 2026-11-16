"""A self-follow doesn't make a user schedulable: the worker's reach excludes the
observer, so the run would fail with NO_ELIGIBLE_USERS. Requires local Neo4j."""

import asyncio

import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

from app.core.config import settings
from app.repos.user_repo import count_user_follows, pubkeys_following_someone

pytestmark = pytest.mark.integration


def _pubkey() -> str:
    return Keys.generate().public_key().to_hex()


def test_self_follow_is_not_a_grapevine():
    self_only, follows_other, other = _pubkey(), _pubkey(), _pubkey()

    async def _run():
        neo4j = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        try:
            async with neo4j.session() as session:
                await session.run(
                    "MERGE (s:NostrUser {pubkey: $s}) MERGE (s)-[:FOLLOWS]->(s) "
                    "MERGE (f:NostrUser {pubkey: $f}) MERGE (o:NostrUser {pubkey: $o}) "
                    "MERGE (f)-[:FOLLOWS]->(f) MERGE (f)-[:FOLLOWS]->(o)",
                    s=self_only,
                    f=follows_other,
                    o=other,
                )
                assert await count_user_follows(session, self_only) == 0
                assert await count_user_follows(session, follows_other) == 1
                assert await pubkeys_following_someone(
                    session, [self_only, follows_other]
                ) == {follows_other}
        finally:
            async with neo4j.session() as session:
                await session.run(
                    "MATCH (u:NostrUser) WHERE u.pubkey IN $pks DETACH DELETE u",
                    pks=[self_only, follows_other, other],
                )
            await neo4j.close()

    asyncio.run(_run())
