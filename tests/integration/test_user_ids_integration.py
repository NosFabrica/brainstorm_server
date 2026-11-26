"""User id translation against a real Scorecard database.

Needs ``SCORECARD_DB_URL`` pointing at a Postgres it may migrate and write to::

    SCORECARD_DB_URL=postgresql+asyncpg://… poetry run pytest tests/integration -m integration
"""
import asyncio
import os
import secrets

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import settings
from app.core.scorecard_database import dispose_scorecard_engine, scorecard_session
from app.repos.user_id_repo import ids_for, pubkeys_for
from tests.test_user_ids import ROOT

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not settings.scorecard_db_url, reason="SCORECARD_DB_URL is not set"
    ),
]


def _new_pubkeys(n: int) -> list[str]:
    return [secrets.token_hex(32) for _ in range(n)]


@pytest.fixture(scope="module", autouse=True)
def migrated():
    config = Config(str(ROOT / "alembic.ini"), ini_section="scorecard")
    config.set_main_option("script_location", str(ROOT / "alembic_scorecard"))
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        command.upgrade(config, "head")
    finally:
        os.chdir(cwd)


def _run(coro_fn):
    async def _wrapped():
        try:
            return await coro_fn()
        finally:
            await dispose_scorecard_engine()

    return asyncio.run(_wrapped())


async def _forget(pubkeys) -> None:
    async with scorecard_session() as db:
        await db.execute(
            text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"), {"p": list(pubkeys)}
        )


def test_new_pubkeys_get_ids_and_keep_them():
    async def _go():
        pubkeys = _new_pubkeys(5)
        try:
            first = await ids_for(pubkeys)
            assert set(first) == set(pubkeys)
            assert len(set(first.values())) == 5
            assert await ids_for(pubkeys) == first
            assert await ids_for(pubkeys + pubkeys) == first
            assert await ids_for(pubkeys, create=False) == first
            assert await pubkeys_for(first.values()) == {
                uid: pk for pk, uid in first.items()
            }
        finally:
            await _forget(pubkeys)

    _run(_go)


def test_reading_without_create_leaves_unknown_pubkeys_out():
    async def _go():
        known, unknown = _new_pubkeys(2), _new_pubkeys(2)
        try:
            ids = await ids_for(known)
            assert await ids_for(known + unknown, create=False) == ids
            assert await ids_for(unknown, create=False) == {}
        finally:
            await _forget(known)

    _run(_go)


def test_a_known_pubkey_does_not_use_up_an_id():
    async def _go():
        pubkeys = _new_pubkeys(50)
        try:
            await ids_for(pubkeys)
            async with scorecard_session() as db:
                before = (
                    await db.execute(text("SELECT last_value FROM nostr_user_id_seq"))
                ).scalar_one()
            for _ in range(3):
                await ids_for(pubkeys)
            async with scorecard_session() as db:
                after = (
                    await db.execute(text("SELECT last_value FROM nostr_user_id_seq"))
                ).scalar_one()
            assert after == before
        finally:
            await _forget(pubkeys)

    _run(_go)


def test_callers_racing_on_the_same_new_pubkeys_get_the_same_ids():
    async def _go():
        pubkeys = _new_pubkeys(200)
        orders = [pubkeys, pubkeys[::-1], pubkeys[100:] + pubkeys[:100]] * 4
        try:
            results = await asyncio.gather(*(ids_for(o) for o in orders))
            assert all(r == results[0] for r in results)
            assert len(set(results[0].values())) == 200
        finally:
            await _forget(pubkeys)

    _run(_go)


def test_a_batch_of_100k_pubkeys():
    async def _go():
        pubkeys = _new_pubkeys(100_000)
        try:
            ids = await ids_for(pubkeys)
            assert len(ids) == 100_000
            assert await ids_for(pubkeys) == ids
            back = await pubkeys_for(ids.values())
            assert len(back) == 100_000
        finally:
            await _forget(pubkeys)

    _run(_go)


def test_the_table_itself_refuses_a_malformed_pubkey():
    async def _go():
        with pytest.raises(Exception):
            async with scorecard_session() as db:
                await db.execute(
                    text("INSERT INTO nostr_user (pubkey) VALUES ('AB')")
                )

    _run(_go)
