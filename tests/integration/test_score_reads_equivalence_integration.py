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

# `a` and `d` are not representable in single precision: the stores must still agree.
SCORES = {
    "a": 0.7512345678901234,
    "b": 0.5,
    "c": 0.125,
    "d": 0.05,
    "r1": 0.5,
    "r2": 0.5,
    # Followers of the subject for the paged reads: ties, every band, and two
    # below the verified line (one of them reported twice, so flagged).
    "t1": 0.3,
    "t2": 0.3,
    "t3": 0.3,
    "high": 0.9,
    "medium": 0.08,
    "below": 0.01,
    "below_flagged": 0.02,
}
UNSCORED = ("unscored", "quiet", "once_reported")
PAGED = ("t1", "t2", "t3", "high", "medium", "below", "below_flagged", "quiet")
VERIFIED_LINE = 0.0625
REPORTER_CUTOFF = 0.25


def _pk() -> str:
    return secrets.token_hex(32)


class Graph:
    def __init__(self):
        self.observer = _pk()
        self.subject = _pk()
        self.people = {name: _pk() for name in (*SCORES, *UNSCORED)}
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
            *((name, "FOLLOWS", "in") for name in (*PAGED, "once_reported")),
            ("t1", "MUTES", "in"),
            ("below", "MUTES", "in"),
            ("high", "REPORTS", "in"),
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
            for target in ("a", "d", "unscored", "below_flagged"):
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
        await s.run(
            "MATCH (x:NostrUser {pubkey: $src}), (y:NostrUser {pubkey: $dst})"
            " MERGE (x)-[:REPORTS]->(y)",
            src=g.people["r1"],
            dst=g.people["once_reported"],
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
    assert postgres[graph.people["a"]] == SCORES["a"]
    assert postgres[graph.people["unscored"]] is None
    assert postgres[graph.no_id] is None


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
    assert neo4j == postgres == [SCORES["a"], None, None, None]


def test_batch_influence_agrees(graph, monkeypatch):
    wanted = [*graph.people.values(), graph.no_id, graph.unknown]

    async def read(session):
        return await user_repo.batch_influence_for_pubkeys(
            session, wanted, graph.observer
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert set(postgres) == set(wanted)
    assert postgres[graph.people["d"]] == SCORES["d"]


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
        graph.people[name]: round(value * 100) / 100
        for name, value in SCORES.items()
        if value >= 0.125
    }
    assert postgres[graph.people["c"]] == 0.12


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
    assert postgres[people["a"]] == row(
        SCORES["a"], True, False
    )  # verified, never flagged
    assert postgres[people["d"]] == row(SCORES["d"], False, True)
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
    # Around the subject: d, unscored and below_flagged; a is reported but verified.
    assert subject.flagged_count == 3
    assert (subject.influence, subject.tier) == (0.25, "medium_high")
    assert subject.flagged_by_observer is False
    assert reported.flagged_by_observer is True
    assert reporter.reporting == 6
    assert unknown == user_repo.OutboundOverview(None, 0, 0, 0, False, 0, None)


def test_a_score_exactly_on_the_line_is_not_verified_in_either_store(
    graph, monkeypatch
):
    """d's Influence equals the line; verified is strictly above it."""

    async def read(session):
        rows = await user_repo.get_trust_signals_for_pubkeys(
            session,
            [graph.people["d"]],
            f"influence_{graph.observer}",
            verified_line=SCORES["d"],
            reporter_cutoff=REPORTER_CUTOFF,
        )
        return rows[graph.people["d"]].verified

    assert _both(graph, monkeypatch, read) == [False, False]


SECTIONS = [
    ("FOLLOWS", "in"),
    ("FOLLOWS", "out"),
    ("MUTES", "in"),
    ("MUTES", "out"),
    ("REPORTS", "in"),
    ("REPORTS", "out"),
]
TIERS = [None, *user_repo._TIER_PREDICATES]


def _walk(read_page):
    """Every page until the cursor runs out: [(items, cursor, total), ...]."""

    async def _all(session):
        pages, cursor = [], (None, None)
        while True:
            items, last, total = await read_page(session, *cursor)
            pages.append((items, last, total))
            if last is None:
                return pages
            cursor = last

    return _all


@pytest.mark.parametrize("rel_type,direction", SECTIONS)
@pytest.mark.parametrize("order", ["desc", "asc"])
def test_connection_pages_agree(graph, monkeypatch, rel_type, direction, order):
    """Every tier filter, with and without verified_only, two rows a page."""

    def pages_for(tier, verified_only):
        async def read_page(session, cursor_inf, cursor_pk):
            return await user_repo.get_paginated_section_connections(
                session,
                graph.subject,
                f"influence_{graph.observer}",
                rel_type,
                direction,
                2,
                cursor_inf,
                cursor_pk,
                verified_cutoff=0.25,
                verified_line=VERIFIED_LINE,
                reporter_cutoff=REPORTER_CUTOFF,
                order=order,
                tier=tier,
                verified_only=verified_only,
                with_total=True,
            )

        return _walk(read_page)

    async def read(session):
        return {
            (tier, verified_only): await pages_for(tier, verified_only)(session)
            for tier in TIERS
            for verified_only in (False, True)
        }

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    if (rel_type, direction) == ("FOLLOWS", "in"):
        everyone = [item for items, _, _ in postgres[(None, False)] for item in items]
        influences = [-1.0 if i.influence is None else i.influence for i in everyone]
        assert influences == sorted(influences, reverse=order == "desc")
        tied = [i.pubkey for i in everyone if i.influence == 0.3]
        assert tied == sorted(tied) and len(tied) == 3
        assert postgres[(None, False)][0][2] == len(everyone) == 13
        flagged = postgres[(user_repo.FLAGGED_TIER, False)]
        assert {i.pubkey for items, _, _ in flagged for i in items} == {
            graph.people["unscored"],
            graph.people["below_flagged"],
        }
        once = next(i for i in everyone if i.pubkey == graph.people["once_reported"])
        assert (once.trusted_reporters, once.tier) == (1, "low")


def test_a_page_without_a_total_agrees(graph, monkeypatch):
    async def read(session):
        return await user_repo.get_paginated_section_connections(
            session,
            graph.subject,
            f"influence_{graph.observer}",
            "FOLLOWS",
            "in",
            50,
            None,
            None,
            verified_cutoff=0.25,
            verified_line=VERIFIED_LINE,
            reporter_cutoff=REPORTER_CUTOFF,
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert postgres[1] is None and postgres[2] is None


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_flagged_pages_agree(graph, monkeypatch, order):
    async def read_page(session, cursor_inf, cursor_pk):
        return await user_repo.get_paginated_flagged_connections(
            session,
            graph.subject,
            f"influence_{graph.observer}",
            2,
            cursor_inf,
            cursor_pk,
            verified_line=VERIFIED_LINE,
            reporter_cutoff=REPORTER_CUTOFF,
            order=order,
            with_total=True,
        )

    neo4j, postgres = _both(graph, monkeypatch, _walk(read_page))
    assert neo4j == postgres
    assert postgres[0][2] == 3
    assert all(
        item.tier == user_repo.FLAGGED_TIER
        for items, _, _ in postgres
        for item in items
    )


def test_section_stats_agree(graph, monkeypatch):
    kinds = [name for name, _, _ in user_repo._STATS_KINDS]

    async def read(session):
        return [
            await user_repo.get_all_section_stats(
                session,
                pk,
                f"influence_{graph.observer}",
                {kind: 0.25 if kind == "followed_by" else 0.0625 for kind in kinds},
                verified_line=VERIFIED_LINE,
                reporter_cutoff=REPORTER_CUTOFF,
            )
            for pk in (graph.subject, graph.unknown)
        ]

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    followed_by = postgres[0]["followed_by"]
    assert followed_by.total == 13
    assert sum(followed_by.tier_counts.model_dump().values()) == 13
    assert getattr(followed_by.tier_counts, user_repo.FLAGGED_TIER) == 2
    assert postgres[1]["followed_by"].total == 0


def test_graph_data_agrees(graph, monkeypatch):
    def _sorted(data):
        return {
            name: sorted(rows, key=lambda r: r["pubkey"])
            if name != "influence"
            else rows
            for name, rows in data.model_dump().items()
        }

    async def read(session):
        return [
            _sorted(
                await user_repo.get_user_graph_data(
                    session,
                    pk,
                    f"influence_{graph.observer}",
                    reporter_cutoff=REPORTER_CUTOFF,
                )
            )
            for pk in (graph.subject, graph.unknown)
        ]

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    assert postgres[0]["influence"] == 0.25
    assert len(postgres[0]["followed_by"]) == 13
    assert postgres[1]["followed_by"] == [] and postgres[1]["influence"] is None


@pytest.mark.parametrize("relation", ["FOLLOWS", "MUTES", "REPORTS"])
@pytest.mark.parametrize("limit", [1, 4, 50])
def test_top_inbound_agrees(graph, monkeypatch, relation, limit):
    async def read(session):
        return await user_repo.get_top_inbound_by_influence(
            session, graph.subject, graph.observer, relation, limit
        )

    neo4j, postgres = _both(graph, monkeypatch, read)
    assert neo4j == postgres
    if relation == "FOLLOWS":
        assert postgres[0].pubkey == graph.people["high"]


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
