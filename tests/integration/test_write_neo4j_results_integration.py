"""Integration tests for the GrapeRank Neo4j result writer.

Requires Neo4j reachable at ``settings.neo4j_db_url``. Postgres is not needed —
the status writes are stubbed; only the Cypher is real.

These pin the three properties a run persists, including that a zero count is
stored like any other, and that the reporter count is not persisted (reads count
`REPORTS` edges live).
"""

import asyncio
from datetime import timedelta
from contextlib import asynccontextmanager
from types import SimpleNamespace
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


def _message(
    observer: str, scorecards: dict, write_set: list[str] | None = None
) -> dict:
    return {
        "private_id": 1,
        "result": {
            "success": True,
            "duration_seconds": 1.0,
            "scorecards": scorecards,
            "neo4jWritePubkeys": write_set,
        },
    }


@pytest.fixture
def graph(monkeypatch):
    """Two observees plus an observer; Postgres stubbed, Neo4j real."""
    observer = Keys.generate().public_key().to_hex()
    pks = {
        "many_followers": Keys.generate().public_key().to_hex(),
        "no_followers": Keys.generate().public_key().to_hex(),
        "unreached": Keys.generate().public_key().to_hex(),
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
    request_row = SimpleNamespace(force_full_neo4j=False)
    monkeypatch.setattr(
        writer_module,
        "select_brainstorm_request_by_id_on_db",
        AsyncMock(return_value=request_row),
    )
    monkeypatch.setattr(writer_module.settings, "neo4j_full_write", False)
    # Cleaned recently by default, so only tests that move the clock see a time-based clear.
    cleanup = {"last": writer_module.utc_now(), "marked": 0}

    async def _last(db, pubkey):
        return cleanup["last"]

    async def _mark(db, pubkey):
        cleanup["marked"] += 1

    monkeypatch.setattr(writer_module, "get_last_neo4j_cleanup_at_on_db", _last)
    monkeypatch.setattr(writer_module, "mark_neo4j_cleanup_on_db", _mark)

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
        yield {"observer": observer, "request_row": request_row, "cleanup": cleanup, **pks}
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


def test_a_run_persists_three_per_observer_properties_but_not_reporters(graph):
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
    assert props["trusted_reporters"] is None


def test_a_zero_follower_count_is_stored_not_skipped(graph):
    """Absence means "no run has written this observer yet" and sends
    /networkAlerts to the graph. A run that computed zero has to say so, or that
    row alone would be counted live against a run-stale reporter count."""
    observer = graph["observer"]

    async def body():
        async with _loop_local_driver():
            await process_neo4j_write_message(
                _message(
                    observer, {"a": _scorecard(observer, graph["no_followers"], 0)}
                )
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


def _write(message: dict):
    async def body():
        async with _loop_local_driver():
            await process_neo4j_write_message(message)

    asyncio.run(body())


def test_a_delta_write_only_touches_the_write_set(graph):
    observer, a, b = graph["observer"], graph["many_followers"], graph["no_followers"]
    first = {a: _scorecard(observer, a, 1), b: _scorecard(observer, b, 1)}
    _write(_message(observer, first))

    second = {a: _scorecard(observer, a, 2), b: _scorecard(observer, b, 2)}
    _write(_message(observer, second, write_set=[a]))

    assert asyncio.run(_props(a, observer))["trusted_followers"] == 2
    assert asyncio.run(_props(b, observer))["trusted_followers"] == 1


def test_a_missing_write_set_writes_every_scorecard(graph):
    observer, a, b = graph["observer"], graph["many_followers"], graph["no_followers"]
    _write(_message(observer, {a: _scorecard(observer, a, 1)}, write_set=[]))
    _write(
        _message(
            observer, {a: _scorecard(observer, a, 3), b: _scorecard(observer, b, 3)}
        )
    )

    assert asyncio.run(_props(a, observer))["trusted_followers"] == 3
    assert asyncio.run(_props(b, observer))["trusted_followers"] == 3


def test_a_full_write_clears_scores_on_nodes_no_longer_reached(graph):
    observer, a, gone = graph["observer"], graph["many_followers"], graph["unreached"]
    _write(
        _message(
            observer,
            {a: _scorecard(observer, a, 1), gone: _scorecard(observer, gone, 1)},
        )
    )

    graph["request_row"].force_full_neo4j = True
    _write(_message(observer, {a: _scorecard(observer, a, 1)}, write_set=[]))

    assert asyncio.run(_props(a, observer))["influence"] == 0.5
    cleared = asyncio.run(_props(gone, observer))
    assert cleared["influence"] is None
    assert cleared["hops"] is None
    assert cleared["trusted_followers"] is None


def test_an_unforced_full_write_keeps_scores_on_nodes_no_longer_reached(graph):
    """The cleanup scan costs a label scan; only forced runs pay it."""
    observer, a, gone = graph["observer"], graph["many_followers"], graph["unreached"]
    _write(
        _message(
            observer,
            {a: _scorecard(observer, a, 1), gone: _scorecard(observer, gone, 1)},
        )
    )
    _write(_message(observer, {a: _scorecard(observer, a, 1)}))

    assert asyncio.run(_props(gone, observer))["influence"] == 0.5


def test_a_hand_deleted_influence_is_rewritten_when_listed(graph):
    observer, a = graph["observer"], graph["many_followers"]
    _write(_message(observer, {a: _scorecard(observer, a, 1)}))

    async def drop():
        driver = _fresh_driver()
        try:
            async with driver.session() as s:
                await s.run(
                    "MATCH (n:NostrUser {pubkey: $pk}) SET n[$k] = null",
                    pk=a,
                    k=f"influence_{observer}",
                )
        finally:
            await driver.close()

    asyncio.run(drop())
    _write(_message(observer, {a: _scorecard(observer, a, 1)}, write_set=[a]))

    assert asyncio.run(_props(a, observer))["influence"] == 0.5


def test_a_delta_write_keeps_scores_on_nodes_no_longer_reached(graph):
    observer, a, gone = graph["observer"], graph["many_followers"], graph["unreached"]
    _write(
        _message(
            observer,
            {a: _scorecard(observer, a, 1), gone: _scorecard(observer, gone, 1)},
        )
    )
    _write(_message(observer, {a: _scorecard(observer, a, 1)}, write_set=[]))

    assert asyncio.run(_props(gone, observer))["influence"] == 0.5


def _seed_then_drop(graph):
    observer, a, gone = graph["observer"], graph["many_followers"], graph["unreached"]
    _write(
        _message(
            observer,
            {a: _scorecard(observer, a, 1), gone: _scorecard(observer, gone, 1)},
        )
    )
    _write(_message(observer, {a: _scorecard(observer, a, 1)}, write_set=[]))
    return observer, a, gone


def test_a_never_cleaned_observer_clears_on_a_delta_run(graph):
    graph["cleanup"]["last"] = None
    observer, a, gone = _seed_then_drop(graph)

    assert asyncio.run(_props(gone, observer))["influence"] is None
    assert asyncio.run(_props(a, observer))["influence"] == 0.5
    assert graph["cleanup"]["marked"] >= 1


def test_an_observer_past_the_interval_clears_on_a_delta_run(graph, monkeypatch):
    monkeypatch.setattr(writer_module.settings, "neo4j_cleanup_interval_days", 30)
    graph["cleanup"]["last"] = writer_module.utc_now() - timedelta(days=31)
    observer, _, gone = _seed_then_drop(graph)

    assert asyncio.run(_props(gone, observer))["influence"] is None
