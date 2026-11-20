"""Ids at ingest against a real Neo4j and a real Scorecard database.

Needs ``SCORECARD_DB_URL``, ``USER_IDS_ENABLED=true`` and the local Neo4j::

    poetry run pytest tests/integration -m integration
"""
import asyncio
import secrets

import pytest
from neo4j import AsyncGraphDatabase
from sqlalchemy import text

from app.core.config import settings
from app.core.redis_db import get_redis_client
from app.core.scorecard_database import dispose_scorecard_engine, scorecard_session
from app.message_queue_tasks import process_strfry_event as mod
from app.neo4j_db.schema import ensure_schema
from app.repos.user_id_repo import ids_for
from tests.integration.test_user_ids_integration import migrated  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (settings.scorecard_db_url and settings.user_ids_enabled),
        reason="needs SCORECARD_DB_URL and USER_IDS_ENABLED=true",
    ),
]


def _pk() -> str:
    return secrets.token_hex(32)


def _kind3(author: str, follows: list[str]) -> dict:
    return {"pubkey": author, "kind": 3, "tags": [["p", pk] for pk in follows]}


async def _nodes(session, pubkeys) -> dict[str, int | None]:
    result = await session.run(
        "MATCH (u:NostrUser) WHERE u.pubkey IN $p RETURN u.pubkey AS pk, u.user_id AS id",
        p=list(pubkeys),
    )
    return {r["pk"]: r["id"] async for r in result}


def _run(scenario):
    async def _wrapped():
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        created: list[str] = []
        # The module's client is bound to the loop that first used it.
        module_redis, mod.redis_client = mod.redis_client, get_redis_client()
        try:
            async with driver.session() as session:
                await ensure_schema(session)
            await scenario(driver, created)
        finally:
            async with driver.session() as session:
                await session.run(
                    "MATCH (u:NostrUser) WHERE u.pubkey IN $p DETACH DELETE u", p=created
                )
            await driver.close()
            async with scorecard_session() as db:
                await db.execute(
                    text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"), {"p": created}
                )
            for pk in created:
                await mod.redis_client.delete(f"{mod.FOLLOWED_BY_KEY_PREFIX}{pk}")
            await mod.redis_client.aclose()
            mod.redis_client = module_redis
            await dispose_scorecard_engine()

    asyncio.run(_wrapped())


def test_a_follow_list_creates_nodes_with_their_ids_and_a_replay_changes_nothing():
    async def scenario(driver, created):
        author, follows = _pk(), [_pk() for _ in range(20)]
        created += [author, *follows]
        event = _kind3(author, follows)

        async with driver.session() as session:
            await mod.process_event_kind_3(session, event)
            first = await _nodes(session, created)
        assert first == await ids_for(created, create=False)
        assert len(set(first.values())) == 21

        async with driver.session() as session:
            await mod.process_event_kind_3(session, event)
            assert await _nodes(session, created) == first

    _run(scenario)


def test_a_node_that_existed_without_an_id_is_given_one():
    async def scenario(driver, created):
        author, old = _pk(), _pk()
        created += [author, old]
        async with driver.session() as session:
            await session.run("CREATE (:NostrUser {pubkey: $p})", p=old)
            await mod.process_event_kind_3(session, _kind3(author, [old]))
            nodes = await _nodes(session, created)
        assert nodes == await ids_for(created, create=False)

    _run(scenario)


def test_a_node_holding_another_id_fails_the_event_and_keeps_its_id():
    async def scenario(driver, created):
        author, clash, fresh = _pk(), _pk(), _pk()
        created += [author, clash, fresh]
        async with driver.session() as session:
            await session.run(
                "CREATE (:NostrUser {pubkey: $p, user_id: $i})", p=clash, i=-7
            )
            with pytest.raises(mod.UserIdMismatch):
                await mod.process_event_kind_3(session, _kind3(author, [fresh, clash]))
        async with driver.session() as session:
            nodes = await _nodes(session, created)
            edges = await (
                await session.run(
                    "MATCH (:NostrUser {pubkey: $a})-[r:FOLLOWS]->() RETURN count(r) AS n",
                    a=author,
                )
            ).single()
        assert nodes == {clash: -7}
        assert edges["n"] == 0

    _run(scenario)


def test_a_graph_failure_after_id_assignment_leaves_no_node_and_an_unused_id(
    monkeypatch,
):
    async def scenario(driver, created):
        author, follows = _pk(), [_pk(), _pk()]
        created += [author, *follows]
        broken = mod._merge_with_ids_cypher("FOLLOWS") + " THIS IS NOT CYPHER"
        monkeypatch.setattr(mod, "_merge_with_ids_cypher", lambda *a, **k: broken)

        async with driver.session() as session:
            with pytest.raises(Exception):
                await mod.process_event_kind_3(session, _kind3(author, follows))
        async with driver.session() as session:
            assert await _nodes(session, created) == {}
        unused = await ids_for(created, create=False)
        assert set(unused) == set(created)

        monkeypatch.undo()
        async with driver.session() as session:
            await mod.process_event_kind_3(session, _kind3(author, follows))
            assert await _nodes(session, created) == unused

    _run(scenario)
