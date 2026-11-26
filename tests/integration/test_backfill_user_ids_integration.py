"""The id backfill script against an isolated Neo4j and Scorecard database.

It reads and writes every NostrUser, so it only runs on an empty graph::

    SCORECARD_DB_URL=… USER_IDS_ENABLED=true NEO4J_DB_URL=… \\
        poetry run pytest tests/integration/test_backfill_user_ids_integration.py -m integration
"""
import asyncio
import secrets

import pytest
from neo4j import AsyncGraphDatabase
from sqlalchemy import text

from app.core.config import settings
from app.core.scorecard_database import dispose_scorecard_engine, scorecard_session
from app.repos.user_id_repo import ids_for, restore_users
from app.repos.user_repo import count_nodes_without_user_id
from scripts import backfill_user_ids as script
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


async def _graph_ids(session) -> dict[str, int | None]:
    result = await session.run("MATCH (u:NostrUser) RETURN u.pubkey AS pk, u.user_id AS id")
    return {r["pk"]: r["id"] async for r in result}


def _run(scenario):
    async def _wrapped():
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        try:
            async with driver.session() as session:
                record = await (
                    await session.run("MATCH (u:NostrUser) RETURN count(u) AS n")
                ).single()
                if record["n"]:
                    pytest.skip("the graph is not empty: isolated stack only")
                async with scorecard_session() as db:
                    await db.execute(text("TRUNCATE nostr_user RESTART IDENTITY"))
                try:
                    await scenario(session)
                finally:
                    await session.run("MATCH (u:NostrUser) DETACH DELETE u")
                    async with scorecard_session() as db:
                        await db.execute(text("TRUNCATE nostr_user RESTART IDENTITY"))
        finally:
            await driver.close()
            await dispose_scorecard_engine()

    asyncio.run(_wrapped())


async def _seed(session, pubkeys):
    await session.run("UNWIND $p AS pk CREATE (:NostrUser {pubkey: pk})", p=pubkeys)


def test_backfill_gives_every_well_formed_node_its_id_and_a_rerun_writes_nothing():
    async def scenario(session):
        plain = [_pk() for _ in range(23)]
        already = _pk()
        malformed = "AB" * 32
        await _seed(session, plain + [malformed])
        kept = (await ids_for([already]))[already]
        await session.run(
            "CREATE (:NostrUser {pubkey: $p, user_id: $i})", p=already, i=kept
        )

        assert await script.counts(session) == (25, 1, 1)

        lines = []
        assert await script.backfill(session, page=10, batch=4, log=lines.append) == 23
        assert len(lines) == 3 and "/s" in lines[0]

        graph = await _graph_ids(session)
        assert graph.pop(malformed) is None
        assert graph == await ids_for(plain + [already], create=False)
        assert graph[already] == kept

        assert await script.backfill(session, page=10, batch=4, log=lines.append) == 0
        assert await count_nodes_without_user_id(session) == (0, 1)
        await _seed(session, [_pk(), _pk()])
        assert await count_nodes_without_user_id(session) == (2, 1)

    _run(scenario)


def test_status_counts_both_sides():
    async def scenario(session):
        nodes = [_pk() for _ in range(6)]
        await _seed(session, nodes)
        await script.backfill(session, page=4, batch=4, log=lambda _: None)
        await _seed(session, [_pk(), "nothex"])
        await ids_for([_pk(), _pk()])  # ids no node carries

        assert await script.status(session) == script.Status(
            nodes=8, with_id=6, malformed=1, table_rows=8, ids_without_node=2
        )

    _run(scenario)


def test_verify_fails_on_a_missing_id_and_on_a_disagreeing_one():
    async def scenario(session):
        nodes = [_pk() for _ in range(9)]
        await _seed(session, nodes + ["AB" * 32])
        before = await script.verify(session, page=4)
        assert (before.without_id, before.malformed, before.ok) == (9, 1, False)

        await script.backfill(session, page=4, batch=4, log=lambda _: None)
        verdict = await script.verify(session, page=4)
        assert verdict.ok and verdict.nodes == 10 and verdict.malformed == 1

        ids = await ids_for(nodes, create=False)
        await session.run(
            "MATCH (u:NostrUser {pubkey: $p}) SET u.user_id = 9000", p=nodes[0]
        )
        unknown = _pk()
        await session.run(
            "CREATE (:NostrUser {pubkey: $p, user_id: 9001})", p=unknown
        )
        verdict = await script.verify(session, page=4)
        assert not verdict.ok
        assert sorted(verdict.disagree) == sorted(
            [(nodes[0], 9000, ids[nodes[0]]), (unknown, 9001, None)]
        )

    _run(scenario)


def test_reconcile_restores_lost_rows_and_moves_the_sequence_past_the_graph():
    async def scenario(session):
        nodes = [_pk() for _ in range(12)]
        await _seed(session, nodes)
        await script.backfill(session, page=5, batch=5, log=lambda _: None)
        before = await ids_for(nodes, create=False)

        # A restore from an older dump: the newest rows and the sequence are gone.
        lost = sorted(before.items(), key=lambda kv: kv[1])[7:]
        async with scorecard_session() as db:
            await db.execute(
                text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"),
                {"p": [pk for pk, _ in lost]},
            )
            await db.execute(text("SELECT setval('nostr_user_id_seq', 7)"))

        plan = await script.plan_reconcile(session, page=5)
        assert not plan.conflicts
        assert sorted(plan.to_insert) == sorted((uid, pk) for pk, uid in lost)
        assert plan.highest_id == max(before.values())

        assert await restore_users(plan.to_insert, plan.highest_id) == len(lost)
        assert await ids_for(nodes, create=False) == before
        assert (await script.verify(session, page=5)).ok

        fresh = _pk()
        assert (await ids_for([fresh]))[fresh] > max(before.values())

        again = await script.plan_reconcile(session, page=5)
        assert again.to_insert == [] and not again.conflicts

    _run(scenario)


def test_reconcile_stops_when_an_id_went_to_another_pubkey():
    async def scenario(session):
        nodes = [_pk() for _ in range(6)]
        await _seed(session, nodes)
        await script.backfill(session, page=5, batch=5, log=lambda _: None)
        before = await ids_for(nodes, create=False)

        # Restored from an old dump, then a new pubkey was handed a lost id.
        victim = max(before, key=before.get)
        async with scorecard_session() as db:
            await db.execute(
                text("DELETE FROM nostr_user WHERE pubkey = :p"), {"p": victim}
            )
            await db.execute(text("SELECT setval('nostr_user_id_seq', 5)"))
        intruder = _pk()
        assert (await ids_for([intruder]))[intruder] == before[victim]

        moved = nodes[0]
        async with scorecard_session() as db:
            await db.execute(
                text("UPDATE nostr_user SET id = 7000 WHERE pubkey = :p"), {"p": moved}
            )

        plan = await script.plan_reconcile(session, page=5)
        assert sorted(plan.conflicts) == sorted(
            [(victim, before[victim], -1), (moved, before[moved], 7000)]
        )
        assert plan.to_insert == []

    _run(scenario)
