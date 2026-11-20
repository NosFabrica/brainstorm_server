"""The one-time score copy against a real Neo4j and Scorecard database.

Needs Neo4j, and a Postgres at SCORECARD_DB_URL. The copy sweeps every user
node, so its progress is reset before each test and its assertions are scoped to
this test's own Observers.
"""
import asyncio
import secrets

import pytest
from neo4j import AsyncGraphDatabase
from sqlalchemy import text

from app.core.config import settings
from app.core.scorecard_database import dispose_scorecard_engine, scorecard_session
from app.repos.scorecard_repo import scores_by_pubkey
from app.repos.user_id_repo import ids_for
from scripts import copy_scores_to_postgres as mod
from tests.integration.test_user_ids_integration import migrated  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not settings.scorecard_db_url, reason="SCORECARD_DB_URL is not set"
    ),
]


def _pk() -> str:
    return secrets.token_hex(32)


class World:
    def __init__(self):
        self.one, self.two = _pk(), _pk()
        self.users = [_pk() for _ in range(25)]
        self.no_id = _pk()

    @property
    def with_ids(self) -> list[str]:
        return [self.one, self.two, *self.users]


def _score(i: int) -> float:
    return 0.5 + i / 1000 + 1e-13  # not representable in single precision


async def _seed(w: World, driver) -> None:
    ids = await ids_for(w.with_ids)
    async with driver.session() as s:
        for pk in w.with_ids:
            await s.run(
                "MERGE (n:NostrUser {pubkey: $pk}) SET n.user_id = $id",
                pk=pk,
                id=ids[pk],
            )
        for i, pk in enumerate(w.users):
            props = {
                f"influence_{w.one}": _score(i),
                f"hops_{w.one}": 2,
                f"trusted_followers_{w.one}": i,
                f"trusted_reporters_{w.one}": 99,
            }
            if i % 2 == 0:
                # Counts stored as floats by an old worker; no follower count.
                props[f"influence_{w.two}"] = 0.25
                props[f"hops_{w.two}"] = 3.0
            await s.run(
                "MATCH (n:NostrUser {pubkey: $pk}) SET n += $props", pk=pk, props=props
            )
        await s.run(
            "MERGE (n:NostrUser {pubkey: $pk}) SET n += $props",
            pk=w.no_id,
            props={f"influence_{w.one}": 0.9},
        )


async def _cleanup(w: World, driver) -> None:
    async with driver.session() as s:
        await s.run(
            "MATCH (n:NostrUser) WHERE n.pubkey IN $pks DETACH DELETE n",
            pks=[*w.with_ids, w.no_id],
        )
    async with scorecard_session() as db:
        await db.execute(
            text(
                "DELETE FROM scorecard s USING nostr_user u"
                " WHERE u.pubkey = ANY(:p) AND u.id IN (s.observer_id, s.observee_id)"
            ),
            {"p": w.with_ids},
        )
        await db.execute(text("DROP TABLE IF EXISTS score_copy_checksum"))
        await db.execute(text("DROP TABLE IF EXISTS score_copy_progress"))
        await db.execute(
            text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"), {"p": w.with_ids}
        )


def _run(test):
    async def _go():
        w = World()
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        try:
            await _seed(w, driver)
            async with driver.session() as session:
                return await test(w, session)
        finally:
            await _cleanup(w, driver)
            await driver.close()
            await dispose_scorecard_engine()

    return asyncio.run(_go())


async def _before(session, w: World) -> dict:
    result = await session.run(
        "MATCH (n:NostrUser) WHERE n.pubkey IN $pks RETURN n.pubkey AS pk, properties(n) AS p",
        pks=[*w.with_ids, w.no_id],
    )
    return {r["pk"]: r["p"] async for r in result}


def test_every_observers_scores_land_and_neo4j_is_untouched():
    async def test(w, session):
        before = await _before(session, w)
        progress = await mod.copy_all(session, page_size=7, log=lambda _: None)
        assert progress.done
        assert await _before(session, w) == before

        one = await scores_by_pubkey(w.one, w.users)
        assert len(one) == 25
        for i, pk in enumerate(w.users):
            assert one[pk] == (_score(i), 2, i)
        two = await scores_by_pubkey(w.two, w.users)
        assert len(two) == 13
        assert two[w.users[0]] == (0.25, 3, None)

        # The node without a user_id could not be copied, and that is reported.
        assert progress.rows_without_user_id >= 1
        assert await mod.mismatches() == []

    _run(test)


def test_a_second_run_copies_nothing_and_an_interrupted_one_resumes():
    async def test(w, session):
        # Stop after the first page: the cursor and that page's checksum are saved.
        calls = {"n": 0}
        real = mod._read_page

        async def one_page_then_fail(s, after, limit):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("interrupted")
            return await real(s, after, limit)

        mod._read_page = one_page_then_fail
        try:
            with pytest.raises(RuntimeError):
                await mod.copy_all(session, page_size=5, log=lambda _: None)
        finally:
            mod._read_page = real
        partial = await mod.read_progress()
        assert partial.nodes == 5 and not partial.done

        resumed = await mod.copy_all(session, page_size=5, log=lambda _: None)
        assert resumed.done
        assert len(await scores_by_pubkey(w.one, w.users)) == 25
        # No page was counted twice.
        assert await mod.mismatches() == []

        again = await mod.copy_all(session, page_size=5, log=lambda _: None)
        assert again.nodes == resumed.nodes

    _run(test)


def test_a_lost_row_is_a_named_mismatch_and_only_repairs_it():
    async def test(w, session):
        await mod.copy_all(session, page_size=50, log=lambda _: None)
        async with scorecard_session() as db:
            await db.execute(
                text(
                    "DELETE FROM scorecard s USING nostr_user o, nostr_user u"
                    " WHERE o.id = s.observer_id AND u.id = s.observee_id"
                    " AND o.pubkey = :o AND u.pubkey = :u"
                ),
                {"o": w.one, "u": w.users[3]},
            )
        bad = await mod.mismatches()
        assert len(bad) == 1 and bad[0].startswith(w.one)

        assert (
            await mod.copy_one(session, w.one, page_size=50, log=lambda _: None) == 25
        )
        assert await mod.mismatches() == []
        assert len(await scores_by_pubkey(w.two, w.users)) == 13

    _run(test)
