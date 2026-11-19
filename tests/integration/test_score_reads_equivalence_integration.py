"""Every converted score read returns the same answer from Neo4j properties
and from the Scorecard database.

Needs Neo4j, and a Postgres at SCORECARD_DB_URL. One small graph is seeded with
the same scores in both stores; each read runs with the setting off, then on.
"""
import asyncio
import secrets

import pytest
from neo4j import AsyncGraphDatabase
from sqlalchemy import text

from app.core.config import settings
from app.core.scorecard_database import dispose_scorecard_engine, scorecard_session
from app.repos import user_repo
from app.repos.scorecard_repo import MissingUserIds
from app.repos.user_id_repo import ids_for
from tests.integration.test_user_ids_integration import migrated  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not settings.scorecard_db_url, reason="SCORECARD_DB_URL is not set"
    ),
]

# Exactly representable, so the stores hold the same number whatever the column type.
SCORES = {"a": 0.75, "b": 0.5, "c": 0.125, "d": 0.03125, "r1": 0.5, "r2": 0.5}
VERIFIED_LINE = 0.0625
REPORTER_CUTOFF = 0.25


def _pk() -> str:
    return secrets.token_hex(32)


class Graph:
    def __init__(self):
        self.observer = _pk()
        self.subject = _pk()
        self.people = {
            name: _pk() for name in ("a", "b", "c", "d", "unscored", "r1", "r2")
        }
        self.no_id = _pk()
        self.unknown = _pk()

    @property
    def all(self) -> list[str]:
        return [self.observer, self.subject, *self.people.values()]


@pytest.fixture
def graph():
    g = Graph()
    yield g


async def _seed(g: Graph, driver) -> None:
    ids = await ids_for(g.all)
    key = f"influence_{g.observer}"
    async with driver.session() as s:
        for pk in g.all:
            await s.run(
                "MERGE (n:NostrUser {pubkey: $pk}) SET n.user_id = $id",
                pk=pk,
                id=ids[pk],
            )
        await s.run("MERGE (n:NostrUser {pubkey: $pk})", pk=g.no_id)
        for name, value in SCORES.items():
            await s.run(
                "MATCH (n:NostrUser {pubkey: $pk}) SET n[$key] = $value",
                pk=g.people[name],
                key=key,
                value=value,
            )
        await s.run(
            "MATCH (n:NostrUser {pubkey: $pk}) SET n[$key] = 0.25",
            pk=g.subject,
            key=key,
        )
        edges = [
            ("a", "FOLLOWS", "in"),
            ("b", "FOLLOWS", "in"),
            ("unscored", "FOLLOWS", "in"),
            ("c", "MUTES", "in"),
            ("d", "REPORTS", "in"),
            ("a", "FOLLOWS", "out"),
            ("c", "FOLLOWS", "out"),
            ("b", "MUTES", "out"),
            ("d", "REPORTS", "out"),
        ]
        for name, rel, direction in edges:
            src, dst = (
                (g.people[name], g.subject)
                if direction == "in"
                else (g.subject, g.people[name])
            )
            await s.run(
                f"MATCH (x:NostrUser {{pubkey: $src}}), (y:NostrUser {{pubkey: $dst}})"
                f" MERGE (x)-[:{rel}]->(y)",
                src=src,
                dst=dst,
            )
        await s.run(
            "MATCH (x:NostrUser {pubkey: $src}), (y:NostrUser {pubkey: $dst})"
            " MERGE (x)-[:FOLLOWS]->(y)",
            src=g.no_id,
            dst=g.subject,
        )
        # Two trusted reporters on a verified, an unverified and an unscored
        # user; c is below the reporter cutoff, so its report never counts.
        for reporter in ("r1", "r2", "c"):
            for target in ("a", "d", "unscored"):
                await s.run(
                    "MATCH (x:NostrUser {pubkey: $src}), (y:NostrUser {pubkey: $dst})"
                    " MERGE (x)-[:REPORTS]->(y)",
                    src=g.people[reporter],
                    dst=g.people[target],
                )
        await s.run(
            "MATCH (x:NostrUser {pubkey: $src}), (y:NostrUser {pubkey: $dst})"
            " MERGE (x)-[:REPORTS]->(y)",
            src=g.people["r1"],
            dst=g.people["b"],
        )
    async with scorecard_session() as db:
        for pk, value in [
            *((g.people[name], value) for name, value in SCORES.items()),
            (g.subject, 0.25),
        ]:
            await db.execute(
                text(
                    "INSERT INTO scorecard VALUES (:o, :e, :influence, 2, 0)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"o": ids[g.observer], "e": ids[pk], "influence": value},
            )


async def _cleanup(g: Graph, driver) -> None:
    async with driver.session() as s:
        await s.run(
            "MATCH (n:NostrUser) WHERE n.pubkey IN $pks DETACH DELETE n",
            pks=[*g.all, g.no_id],
        )
    async with scorecard_session() as db:
        await db.execute(
            text(
                "DELETE FROM scorecard s USING nostr_user u"
                " WHERE u.pubkey = ANY(:p) AND u.id IN (s.observer_id, s.observee_id)"
            ),
            {"p": g.all},
        )
        await db.execute(
            text("DELETE FROM nostr_user WHERE pubkey = ANY(:p)"), {"p": g.all}
        )


def _both(g: Graph, monkeypatch, read):
    """`read(session)` with the setting off, then on, over the seeded graph."""

    async def _run():
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        try:
            await _seed(g, driver)
            answers = []
            for on in (False, True):
                monkeypatch.setattr(user_repo.settings, "scores_in_postgres", on)
                async with driver.session() as session:
                    answers.append(await read(session))
            return answers
        finally:
            await _cleanup(g, driver)
            await driver.close()
            await dispose_scorecard_engine()

    return asyncio.run(_run())


def _by_pubkey(connections) -> dict:
    return {c.pubkey: c.influence for c in connections}


LISTS = [
    "get_list_of_pubkeys_following_user",
    "get_list_of_pubkeys_that_user_follows",
    "get_list_of_pubkeys_muting_user",
    "get_list_of_pubkeys_that_user_mutes",
    "get_list_of_pubkeys_reporting_user",
    "get_list_of_pubkeys_that_user_reports",
]


@pytest.mark.parametrize("name", LISTS)
def test_the_connection_lists_agree(graph, monkeypatch, name):
    async def read(session):
        return _by_pubkey(
            await getattr(user_repo, name)(session, graph.subject, graph.observer)
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert neo4j


def test_followers_include_the_unscored_and_the_node_without_an_id(graph, monkeypatch):
    async def read(session):
        return _by_pubkey(
            await user_repo.get_list_of_pubkeys_following_user(
                session, graph.subject, graph.observer
            )
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert postgres == {
        graph.people["a"]: 0.75,
        graph.people["b"]: 0.5,
        graph.people["unscored"]: None,
        graph.no_id: None,
    }


def test_a_list_with_no_observer_reads_the_subjects_own_scores(graph, monkeypatch):
    async def read(session):
        return _by_pubkey(
            await user_repo.get_list_of_pubkeys_that_user_follows(
                session, graph.observer
            )
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres == {}


def test_single_influence_agrees(graph, monkeypatch):
    async def read(session):
        return [
            await user_repo.get_influence_for_observer(session, pk, graph.observer)
            for pk in (
                graph.people["a"],
                graph.people["unscored"],
                graph.no_id,
                graph.unknown,
            )
        ]

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres == [0.75, None, None, None]


def test_batch_influence_agrees(graph, monkeypatch):
    wanted = [*graph.people.values(), graph.no_id, graph.unknown]

    async def read(session):
        return await user_repo.batch_influence_for_pubkeys(
            session, wanted, graph.observer
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert set(postgres) == set(wanted)
    assert postgres[graph.people["d"]] == 0.03125


def test_qualifying_asserters_agree(graph, monkeypatch):
    wanted = [*graph.people.values(), graph.unknown]

    async def read(session):
        return await user_repo.get_qualifying_asserters_for_observer(
            session, wanted, graph.observer, 0.125
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    # Inclusive floor, weight quantized to the Rank quantum.
    assert postgres == {
        graph.people["a"]: 0.75,
        graph.people["b"]: 0.5,
        graph.people["c"]: 0.12,
        graph.people["r1"]: 0.5,
        graph.people["r2"]: 0.5,
    }


def test_counts_and_influence_agree(graph, monkeypatch):
    async def read(session):
        key = f"influence_{graph.observer}"
        return [
            await user_repo.get_counts_and_influence(session, graph.subject, key),
            await user_repo.get_counts_and_influence(session, graph.unknown, key),
        ]

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert postgres[0] == user_repo.OutboundCounts(0.25, 2, 1, 1)
    assert postgres[1] == user_repo.OutboundCounts(None, 0, 0, 0)


def test_trust_signals_agree(graph, monkeypatch):
    wanted = [*graph.people.values(), graph.subject, graph.no_id, graph.unknown]

    async def read(session):
        return await user_repo.get_trust_signals_for_pubkeys(
            session,
            wanted,
            f"influence_{graph.observer}",
            verified_line=VERIFIED_LINE,
            reporter_cutoff=REPORTER_CUTOFF,
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    row = user_repo.TrustSignalRow
    people = graph.people
    assert postgres[people["a"]] == row(0.75, True, False)  # verified, never flagged
    assert postgres[people["d"]] == row(0.03125, False, True)
    assert postgres[people["unscored"]] == row(None, False, True)
    assert postgres[people["b"]] == row(0.5, True, False)
    assert postgres[graph.unknown] == row(None, False, False)


def test_the_overview_agrees(graph, monkeypatch):
    async def read(session):
        return [
            await user_repo.get_outbound_counts_and_influence(
                session,
                pk,
                f"influence_{graph.observer}",
                verified_line=VERIFIED_LINE,
                reporter_cutoff=REPORTER_CUTOFF,
            )
            for pk in (
                graph.subject,
                graph.people["d"],
                graph.people["r1"],
                graph.unknown,
            )
        ]

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    subject, reported, reporter, unknown = postgres
    # Around the subject: d and unscored are flagged; a is reported but verified.
    assert subject.flagged_count == 2
    assert (subject.influence, subject.tier) == (0.25, "medium_high")
    assert subject.flagged_by_observer is False
    assert reported.flagged_by_observer is True
    assert reporter.reporting == 4
    assert unknown == user_repo.OutboundOverview(None, 0, 0, 0, False, 0, None)


def test_a_graph_id_missing_from_the_table_fails_the_read(graph, monkeypatch):
    async def read(session):
        if not user_repo.settings.scores_in_postgres:
            return None
        await session.run(
            "MATCH (n:NostrUser {pubkey: $pk}) SET n.user_id = 2147483000",
            pk=graph.people["a"],
        )
        with pytest.raises(MissingUserIds):
            await user_repo.get_list_of_pubkeys_following_user(
                session, graph.subject, graph.observer
            )
        return "raised"

    assert _both(graph, monkeypatch, read) == [None, "raised"]
