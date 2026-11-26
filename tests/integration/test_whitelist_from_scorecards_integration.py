"""The whitelist read from Scorecards answers exactly as the stored snapshot did.

Needs a Postgres at SCORECARD_DB_URL. The old snapshot table is created in that
same database for the comparison, so no app database is needed.
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
from app.message_queue_tasks.message_queue_consumer import (
    bucket_scorecards_by_confidence_and_hops,
)
from app.message_queue_tasks.write_neo4j_results import process_neo4j_write_message
from app.repos.observer_whitelist_repo import (
    select_observer_whitelist_updated_at,
    upsert_observer_whitelist_on_db,
)
from app.repos.scorecard_repo import clear_scores_about_on_db
from app.repos.user_id_repo import ids_for
from app.services import user_service
from tests.integration.test_user_ids_integration import migrated  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not settings.scorecard_db_url, reason="SCORECARD_DB_URL is not set"
    ),
]

# Around the 0.02 validity cutoff and the two-decimal rounding, including values
# Postgres and Python round differently (0.015, 0.025, 0.125) and one exactly on it.
INFLUENCES = [0.02, 0.0199, 0.015, 0.0150001, 0.025, 0.0249, 0.125, 0.5, 0.9999, 0.0]


def _pk() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def writer(monkeypatch):
    @asynccontextmanager
    async def _fake_db_session():
        yield AsyncMock()

    monkeypatch.setattr(writer_module, "db_session", _fake_db_session)
    for name, value in {
        "update_brainstorm_request_internal_publication_status_by_id_on_db": None,
        "select_brainstorm_request_by_id_on_db": SimpleNamespace(
            force_full_neo4j=False
        ),
        "select_compromised_pubkeys_on_db": set(),
        "get_last_neo4j_cleanup_at_on_db": writer_module.utc_now(),
        "mark_neo4j_cleanup_on_db": None,
    }.items():
        monkeypatch.setattr(writer_module, name, AsyncMock(return_value=value))
    monkeypatch.setattr(writer_module.settings, "scores_in_postgres", True)
    monkeypatch.setattr(settings, "cutoff_of_valid_graperank_scores", 0.02)


def _go(test):
    async def _run():
        observer = _pk()
        observees = [_pk() for _ in INFLUENCES]
        await ids_for([observer, *observees])
        # The snapshot table as the app database has it, less its foreign key.
        async with scorecard_session() as db:
            await db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS observerwhitelist ("
                    " observer_pubkey varchar PRIMARY KEY,"
                    " scores jsonb NOT NULL DEFAULT '{}'::jsonb,"
                    " last_request_id integer,"
                    " created_at timestamp NOT NULL DEFAULT now(),"
                    " updated_at timestamp NOT NULL DEFAULT now())"
                )
            )
        try:
            return await test(observer, observees)
        finally:
            async with scorecard_session() as db:
                await db.execute(
                    text(
                        "DELETE FROM scorecard s USING nostr_user u"
                        " WHERE u.pubkey = ANY(:p) AND u.id IN (s.observer_id, s.observee_id)"
                    ),
                    {"p": [observer, *observees]},
                )
                await db.execute(
                    text(
                        "DELETE FROM scorecard_observer o USING nostr_user u"
                        " WHERE u.pubkey = :p AND u.id = o.observer_id"
                    ),
                    {"p": observer},
                )
                await db.execute(text("DROP TABLE IF EXISTS observerwhitelist"))
                await db.execute(
                    text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"),
                    {"p": [observer, *observees]},
                )
            await dispose_scorecard_engine()

    return asyncio.run(_run())


def _scorecards(observer, observees) -> dict:
    return {
        observee: {
            "observer": observer,
            "observee": observee,
            "influence": influence,
            "hops": 2,
            "trusted_followers": 0,
        }
        for observee, influence in zip(observees, INFLUENCES)
    }


async def _both(observer, threshold, monkeypatch_settings):
    """The snapshot's answer, then the Scorecards' answer."""
    answers = []
    for on in (False, True):
        monkeypatch_settings(on)
        async with scorecard_session() as db:
            answers.append(
                await user_service.get_whitelisted_pubkeys_of_observer(
                    db, observer, threshold
                )
            )
    return answers


@pytest.mark.parametrize("threshold", [0.02, 0.03, 0.13, 0.5, 1.0])
def test_the_list_is_the_snapshots_list(writer, monkeypatch, threshold):
    def flag(on):
        monkeypatch.setattr(user_service.settings, "scores_in_postgres", on)

    async def test(observer, observees):
        scorecards = _scorecards(observer, observees)
        # What the results consumer stored for this run...
        _, snapshot = bucket_scorecards_by_confidence_and_hops(scorecards, 0.02)
        async with scorecard_session() as db:
            await upsert_observer_whitelist_on_db(db, observer, snapshot, 1)
        # ...and what the score writer stored for the same run.
        await process_neo4j_write_message(
            {"private_id": 1, "result": {"success": True, "scorecards": scorecards}}
        )
        from_snapshot, from_scorecards = await _both(observer, threshold, flag)
        # Same members, and the same (pubkey) order the JSON map came back in.
        assert from_scorecards == from_snapshot
        return from_scorecards

    listed = _go(test)
    if threshold == 0.02:
        # 0.02 exactly is in; 0.0199 and 0.015 round to 0.02 and 0.01.
        assert len(listed) == 8


def test_no_scorecards_is_no_whitelist_and_a_write_stamps_it(writer, monkeypatch):
    async def test(observer, observees):
        monkeypatch.setattr(user_service.settings, "scores_in_postgres", True)
        async with scorecard_session() as db:
            assert await user_service.whitelist_updated_at(db, observer) is None
            assert (
                await user_service.get_whitelisted_pubkeys_of_observer(db, observer)
                == []
            )
        await process_neo4j_write_message(
            {
                "private_id": 1,
                "result": {
                    "success": True,
                    "scorecards": _scorecards(observer, observees),
                },
            }
        )
        async with scorecard_session() as db:
            first = await user_service.whitelist_updated_at(db, observer)
        assert first is not None

        # Marking an account compromised removes it and moves the stamp, so a
        # cached answer is not served again.
        victim = observees[INFLUENCES.index(0.5)]
        async with scorecard_session() as db:
            assert await clear_scores_about_on_db(db, victim) == 1
        async with scorecard_session() as db:
            assert await user_service.whitelist_updated_at(db, observer) > first
            listed = await user_service.get_whitelisted_pubkeys_of_observer(
                db, observer
            )
        assert victim not in listed and len(listed) == 7

    _go(test)


def test_with_the_setting_off_the_snapshot_is_still_the_source(writer, monkeypatch):
    async def test(observer, observees):
        monkeypatch.setattr(user_service.settings, "scores_in_postgres", False)
        async with scorecard_session() as db:
            await upsert_observer_whitelist_on_db(db, observer, {observees[0]: 0.5}, 1)
        async with scorecard_session() as db:
            assert await user_service.get_whitelisted_pubkeys_of_observer(
                db, observer
            ) == [observees[0]]
            stored = await select_observer_whitelist_updated_at(db, observer)
            assert await user_service.whitelist_updated_at(db, observer) == stored

    _go(test)
