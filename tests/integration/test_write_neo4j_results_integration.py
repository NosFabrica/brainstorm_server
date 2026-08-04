"""Integration tests for the GrapeRank Neo4j result writer.

Requires Neo4j reachable at ``settings.neo4j_db_url``. Postgres is not needed —
the status writes are stubbed; only the Cypher is real.

PR #59 added `trusted_followers_<observer>` to the writer's SET clause without a
test, and no test file for this module existed. These pin the four properties a
run persists, including that a zero count is stored like any other: /networkAlerts
reads the reporter count from the same run, so both sides of its threshold stay
on one clock.

Issue: .scratch/network-alerts/issues/01-preset-drive-alerts.md
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

import app.message_queue_tasks.write_neo4j_results as writer_module
from app.core.config import settings
from app.message_queue_tasks.write_neo4j_results import process_neo4j_write_message

pytestmark = pytest.mark.integration


def _fresh_driver():
    return AsyncGraphDatabase.driver(
        settings.neo4j_db_url,
        auth=(settings.neo4j_db_username, settings.neo4j_db_password),
    )


def _scorecard(observer: str, observee: str, trusted_followers: int) -> dict:
    return {
        "observer": observer,
        "observee": observee,
        "influence": 0.5,
        "hops": 2,
        "trusted_followers": trusted_followers,
        "trusted_reporters": 3,
        "trusted_muters": 0,
    }


def _message(observer: str, scorecards: dict) -> dict:
    return {
        "private_id": 1,
        "result": {
            "success": True,
            "duration_seconds": 1.0,
            "scorecards": scorecards,
        },
    }


@pytest.fixture
def graph(monkeypatch):
    """Two observees plus an observer; Postgres stubbed, Neo4j real."""
    observer = Keys.generate().public_key().to_hex()
    pks = {
        "many_followers": Keys.generate().public_key().to_hex(),
        "no_followers": Keys.generate().public_key().to_hex(),
    }

    @asynccontextmanager
    async def _fake_db_session():
        yield AsyncMock()

    monkeypatch.setattr(writer_module, "db_session", _fake_db_session)
    monkeypatch.setattr(
        writer_module,
        "update_brainstorm_request_internal_publication_status_by_id_on_db",
        AsyncMock(),
    )

    async def _seed():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                for pk in pks.values():
                    await s.run("MERGE (n:NostrUser {pubkey: $pk})", pk=pk)
        finally:
            await driver.close()

    async def _teardown():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                await s.run(
                    "MATCH (n:NostrUser) WHERE n.pubkey IN $pks DETACH DELETE n",
                    pks=list(pks.values()),
                )
        finally:
            await driver.close()

    asyncio.run(_seed())
    try:
        yield {"observer": observer, **pks}
    finally:
        asyncio.run(_teardown())


@asynccontextmanager
async def _loop_local_driver():
    """The writer's module-level driver pins connections to the loop that first
    used it; each test body is its own ``asyncio.run``. Restore the original so
    later tests don't inherit a closed driver."""
    driver = _fresh_driver()
    original = writer_module.neo4j_driver
    writer_module.neo4j_driver = driver
    try:
        yield
    finally:
        writer_module.neo4j_driver = original
        await driver.close()


async def _props(pubkey: str, observer: str) -> dict:
    driver = _fresh_driver()
    try:
        async with driver.session() as s:
            result = await s.run(
                """
                MATCH (n:NostrUser {pubkey: $pk})
                RETURN n[$inf] AS influence, n[$hops] AS hops,
                       n[$tf] AS trusted_followers, n[$tr] AS trusted_reporters
                """,
                pk=pubkey,
                inf=f"influence_{observer}",
                hops=f"hops_{observer}",
                tf=f"trusted_followers_{observer}",
                tr=f"trusted_reporters_{observer}",
            )
            record = await result.single()
            return dict(record) if record else {}
    finally:
        await driver.close()


def test_a_run_persists_all_four_per_observer_properties(graph):
    observer = graph["observer"]

    async def body():
        async with _loop_local_driver():
            await process_neo4j_write_message(
                _message(
                    observer, {"a": _scorecard(observer, graph["many_followers"], 7)}
                )
            )
        return await _props(graph["many_followers"], observer)

    props = asyncio.run(body())
    assert props["influence"] == 0.5
    assert props["hops"] == 2
    assert props["trusted_followers"] == 7
    assert props["trusted_reporters"] == 3


def test_a_zero_follower_count_is_stored_not_skipped(graph):
    """Absence means "no run has written this observer yet" and sends
    /networkAlerts to the graph. A run that computed zero has to say so, or that
    row alone would be counted live against a run-stale reporter count."""
    observer = graph["observer"]

    async def body():
        async with _loop_local_driver():
            await process_neo4j_write_message(
                _message(observer, {"a": _scorecard(observer, graph["no_followers"], 0)})
            )
        return await _props(graph["no_followers"], observer)

    props = asyncio.run(body())
    assert props["trusted_followers"] == 0


def test_a_single_scorecard_run_does_not_raise(graph):
    """`islice(…, 1, 2)` took the *second* scorecard, so a run with exactly one
    — a new observer with a trivial graph — raised StopIteration after doing all
    its writes."""
    observer = graph["observer"]

    async def body():
        async with _loop_local_driver():
            await process_neo4j_write_message(
                _message(
                    observer, {"a": _scorecard(observer, graph["many_followers"], 1)}
                )
            )

    asyncio.run(body())
