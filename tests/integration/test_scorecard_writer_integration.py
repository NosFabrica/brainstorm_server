"""The results writer storing Scorecards in the Scorecard database.

Needs a Postgres at SCORECARD_DB_URL (the table is created here). Neo4j and the
app database are not used: the status writes are stubbed, and with the setting
on the writer must not touch Neo4j at all.
"""
import asyncio
import secrets
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

import app.message_queue_tasks.write_neo4j_results as writer_module
from app.core.config import settings
from app.core.scorecard_database import dispose_scorecard_engine, scorecard_session
from app.message_queue_tasks.write_neo4j_results import process_neo4j_write_message
from app.repos.scorecard_repo import (
    MissingUserIds,
    get_pubkeys_scored_by_observer_on_db,
    observer_id_on_db,
)
from app.repos.user_id_repo import ids_for
from app.services import compromised_service
from tests.integration.test_user_ids_integration import migrated  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not settings.scorecard_db_url, reason="SCORECARD_DB_URL is not set"
    ),
]


def _pk() -> str:
    return secrets.token_hex(32)


def _card(observer: str, observee: str, influence=0.5, hops=2, followers=7) -> dict:
    return {
        "observer": observer,
        "observee": observee,
        "influence": influence,
        "hops": hops,
        "trusted_followers": followers,
        "trusted_reporters": 3,
        "trusted_muters": 0,
    }


def _message(cards, write_set=None, clear_set=None, floor=None) -> dict:
    result = {
        "success": True,
        "scorecards": {c["observee"]: c for c in cards},
        "neo4jWritePubkeys": write_set,
    }
    if clear_set is not None:
        result["neo4jClearPubkeys"] = clear_set
    if floor is not None:
        result["neo4jPersistFloor"] = floor
    return {"private_id": 1, "result": result}


@pytest.fixture
def run(monkeypatch):
    """The writer with the setting on, the app database stubbed, and any Neo4j
    score write failing the test."""

    @asynccontextmanager
    async def _fake_db_session():
        yield AsyncMock()

    request_row = SimpleNamespace(force_full_neo4j=False)
    state = {"request_row": request_row, "last_cleanup": writer_module.utc_now()}
    monkeypatch.setattr(writer_module, "db_session", _fake_db_session)
    monkeypatch.setattr(
        writer_module,
        "update_brainstorm_request_internal_publication_status_by_id_on_db",
        AsyncMock(),
    )
    monkeypatch.setattr(
        writer_module,
        "select_brainstorm_request_by_id_on_db",
        AsyncMock(return_value=request_row),
    )
    monkeypatch.setattr(
        writer_module, "select_compromised_pubkeys_on_db", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        writer_module,
        "get_last_neo4j_cleanup_at_on_db",
        AsyncMock(side_effect=lambda db, pk: state["last_cleanup"]),
    )
    monkeypatch.setattr(writer_module, "mark_neo4j_cleanup_on_db", AsyncMock())
    monkeypatch.setattr(writer_module.settings, "scores_in_postgres", True)
    monkeypatch.setattr(writer_module.settings, "neo4j_full_write", False)
    for name in (
        "set_observer_scores",
        "clear_observer_scores",
        "get_pubkeys_scored_by_observer",
    ):
        monkeypatch.setattr(
            writer_module,
            name,
            AsyncMock(side_effect=AssertionError(f"{name} wrote to Neo4j")),
        )
    return state


def _go(coro_fn):
    async def _wrapped():
        try:
            return await coro_fn()
        finally:
            await dispose_scorecard_engine()

    return asyncio.run(_wrapped())


async def _stored(observer: str) -> dict[str, tuple]:
    async with scorecard_session() as db:
        rows = await db.execute(
            text(
                """
                SELECT u.pubkey, s.influence, s.hops, s.verified_followers
                FROM scorecard s
                JOIN nostr_user o ON o.id = s.observer_id
                JOIN nostr_user u ON u.id = s.observee_id
                WHERE o.pubkey = :observer
                """
            ),
            {"observer": observer},
        )
        return {r[0]: (round(r[1], 4), r[2], r[3]) for r in rows}


async def _forget(pubkeys) -> None:
    async with scorecard_session() as db:
        await db.execute(
            text(
                "DELETE FROM scorecard s USING nostr_user u"
                " WHERE u.pubkey = ANY(:p) AND u.id IN (s.observer_id, s.observee_id)"
            ),
            {"p": list(pubkeys)},
        )
        await db.execute(
            text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"), {"p": list(pubkeys)}
        )


def test_a_first_run_stores_every_listed_scorecard(run):
    async def _test():
        observer, a, b = _pk(), _pk(), _pk()
        await ids_for([observer, a, b])
        try:
            cards = [_card(observer, a, 0.9, 1, 12), _card(observer, b, 0.25, 3, 0)]
            await process_neo4j_write_message(_message(cards, write_set=[a, b]))
            assert await _stored(observer) == {a: (0.9, 1, 12), b: (0.25, 3, 0)}
        finally:
            await _forget([observer, a, b])

    _go(_test)


def test_a_delta_run_only_touches_the_write_set(run):
    async def _test():
        observer, a, b = _pk(), _pk(), _pk()
        await ids_for([observer, a, b])
        try:
            first = [_card(observer, a, 0.9), _card(observer, b, 0.25)]
            await process_neo4j_write_message(_message(first, write_set=[a, b]))
            second = [_card(observer, a, 0.1), _card(observer, b, 0.75)]
            await process_neo4j_write_message(_message(second, write_set=[b]))
            stored = await _stored(observer)
            assert stored[a][0] == 0.9
            assert stored[b][0] == 0.75
        finally:
            await _forget([observer, a, b])

    _go(_test)


def test_a_missing_write_set_writes_every_scorecard_above_the_floor(run):
    async def _test():
        observer, a, b = _pk(), _pk(), _pk()
        await ids_for([observer, a, b])
        try:
            cards = [_card(observer, a, 0.9), _card(observer, b, 0.00005)]
            await process_neo4j_write_message(_message(cards, floor=0.0001))
            assert set(await _stored(observer)) == {a}
        finally:
            await _forget([observer, a, b])

    _go(_test)


def test_the_clear_set_deletes_rows(run):
    async def _test():
        observer, a, b = _pk(), _pk(), _pk()
        await ids_for([observer, a, b])
        try:
            cards = [_card(observer, a), _card(observer, b)]
            await process_neo4j_write_message(_message(cards, write_set=[a, b]))
            # A pubkey with no id has no row to clear; it is not an error.
            await process_neo4j_write_message(
                _message(cards, write_set=[], clear_set=[b, _pk()])
            )
            assert set(await _stored(observer)) == {a}
        finally:
            await _forget([observer, a, b])

    _go(_test)


def test_a_forced_run_deletes_scores_for_users_no_longer_reached(run):
    async def _test():
        observer, a, gone = _pk(), _pk(), _pk()
        await ids_for([observer, a, gone])
        try:
            first = [_card(observer, a), _card(observer, gone)]
            await process_neo4j_write_message(_message(first, write_set=[a, gone]))
            # Unforced and cleaned recently: the unreached row stays.
            await process_neo4j_write_message(
                _message([_card(observer, a)], write_set=[])
            )
            assert set(await _stored(observer)) == {a, gone}
            run["request_row"].force_full_neo4j = True
            await process_neo4j_write_message(
                _message([_card(observer, a)], write_set=[])
            )
            assert set(await _stored(observer)) == {a}
        finally:
            await _forget([observer, a, gone])

    _go(_test)


def test_an_observer_past_the_cleanup_interval_clears_on_a_delta_run(run):
    async def _test():
        observer, a, gone = _pk(), _pk(), _pk()
        await ids_for([observer, a, gone])
        try:
            first = [_card(observer, a), _card(observer, gone)]
            await process_neo4j_write_message(_message(first, write_set=[a, gone]))
            run["last_cleanup"] = None
            await process_neo4j_write_message(
                _message([_card(observer, a)], write_set=[])
            )
            assert set(await _stored(observer)) == {a}
            writer_module.mark_neo4j_cleanup_on_db.assert_awaited()
        finally:
            await _forget([observer, a, gone])

    _go(_test)


def test_a_scored_pubkey_without_an_id_fails_the_run_and_writes_nothing(run):
    async def _test():
        observer, known, unknown = _pk(), _pk(), _pk()
        await ids_for([observer, known])
        try:
            cards = [_card(observer, known), _card(observer, unknown)]
            with pytest.raises(MissingUserIds):
                await process_neo4j_write_message(
                    _message(cards, write_set=[known, unknown])
                )
            assert await _stored(observer) == {}
        finally:
            await _forget([observer, known])

    _go(_test)


def test_an_observer_without_an_id_fails_the_run(run):
    async def _test():
        observer, a = _pk(), _pk()
        await ids_for([a])
        try:
            with pytest.raises(MissingUserIds):
                await process_neo4j_write_message(
                    _message([_card(observer, a)], write_set=[a])
                )
        finally:
            await _forget([a])

    _go(_test)


def test_two_observers_writing_at_once_keep_their_own_rows(run):
    async def _test():
        one, two = _pk(), _pk()
        shared = [_pk() for _ in range(200)]
        await ids_for([one, two, *shared])
        try:
            await asyncio.gather(
                process_neo4j_write_message(
                    _message([_card(one, pk, 0.9) for pk in shared], write_set=shared)
                ),
                process_neo4j_write_message(
                    _message([_card(two, pk, 0.2) for pk in shared], write_set=shared)
                ),
            )
            first, second = await _stored(one), await _stored(two)
            assert len(first) == len(second) == 200
            assert {v[0] for v in first.values()} == {0.9}
            assert {v[0] for v in second.values()} == {0.2}
        finally:
            await _forget([one, two, *shared])

    _go(_test)


def test_rewriting_the_same_values_changes_no_row(run):
    async def _xmin(observer: str) -> str:
        async with scorecard_session() as db:
            oid = await observer_id_on_db(db, observer)
            assert len(await get_pubkeys_scored_by_observer_on_db(db, oid)) == 1
            return (
                await db.execute(
                    text("SELECT xmin::text FROM scorecard WHERE observer_id = :o"),
                    {"o": oid},
                )
            ).scalar_one()

    async def _test():
        observer, a = _pk(), _pk()
        await ids_for([observer, a])
        try:
            message = _message([_card(observer, a)], write_set=[a])
            await process_neo4j_write_message(message)
            before = await _xmin(observer)
            await process_neo4j_write_message(message)
            # Same row version: the unchanged upsert left no dead tuple.
            assert await _xmin(observer) == before
        finally:
            await _forget([observer, a])

    _go(_test)


def test_a_compromised_account_loses_every_observers_score(run, monkeypatch):
    async def _test():
        one, two, victim, other = _pk(), _pk(), _pk(), _pk()
        await ids_for([one, two, victim, other])
        try:
            for observer in (one, two):
                await process_neo4j_write_message(
                    _message(
                        [_card(observer, victim), _card(observer, other)],
                        write_set=[victim, other],
                    )
                )
            monkeypatch.setattr(
                compromised_service.settings, "scores_in_postgres", True
            )
            assert await compromised_service.clear_stored_scores(victim) == 2
            assert set(await _stored(one)) == {other}
            assert set(await _stored(two)) == {other}
        finally:
            await _forget([one, two, victim, other])

    _go(_test)
