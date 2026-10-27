"""kind-10040 ingestion: an author's designations become `DESIGNATES` edges and
the `designates:<author>` set GrapeRank reads, so every Observer whose graph
reaches the author sees them — not only the author's own run."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.message_queue_tasks import process_strfry_event as mod
from app.message_queue_tasks.process_strfry_event import process_strfry_event

AUTHOR = "a" * 64
PROVIDER = "b" * 64
OTHER_PROVIDER = "c" * 64


def _event(tags: list) -> dict:
    return {"kind": 10040, "pubkey": AUTHOR, "tags": tags, "content": ""}


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.run = AsyncMock()

    # The session doubles as the transaction.
    async def execute_write(work, *args, **kwargs):
        return await work(session, *args, **kwargs)

    session.execute_write = AsyncMock(side_effect=execute_write)
    return session


class _Pipe:
    def __init__(self, log: list, transaction: bool):
        self.log = log
        self.transaction = transaction
        self.ops: list = []

    def delete(self, key):
        self.ops.append(("delete", key))

    def sadd(self, key, *members):
        self.ops.append(("sadd", key, *members))

    async def execute(self):
        self.log.append((self.transaction, self.ops))


def _ingest(monkeypatch, event: dict):
    log: list = []
    redis = MagicMock()
    redis.pipeline = lambda transaction: _Pipe(log, transaction)
    monkeypatch.setattr(mod, "redis_client", redis)
    session = _mock_session()
    asyncio.run(process_strfry_event(session, event))
    return session, log


def test_designations_become_edges_and_the_forward_set(monkeypatch):
    session, log = _ingest(
        monkeypatch,
        _event(
            [
                ["30382:rank", PROVIDER, "wss://nip85.brainstorm.world"],
                ["30382:followers", PROVIDER, "wss://nip85.brainstorm.world"],
                ["30392", OTHER_PROVIDER],
                ["p", "d" * 64],
            ]
        ),
    )

    session.run.assert_awaited_once()
    cypher = session.run.await_args.args[0]
    assert "MERGE (pub)-[:DESIGNATES]->(d)" in cypher
    assert "DELETE r" in cypher  # dropped designations go in the same write
    assert session.run.await_args.kwargs == {
        "publisher": AUTHOR,
        "designated": [PROVIDER, OTHER_PROVIDER],
    }
    assert log == [
        (
            True,
            [
                ("delete", f"designates:{AUTHOR}"),
                ("sadd", f"designates:{AUTHOR}", PROVIDER, OTHER_PROVIDER),
            ],
        )
    ]


def test_an_empty_designation_clears_edges_and_set(monkeypatch):
    session, log = _ingest(monkeypatch, _event([["p", PROVIDER]]))

    cypher = session.run.await_args.args[0]
    assert "MERGE" not in cypher  # never creates a node for a list that names nobody
    assert "DELETE r" in cypher
    assert log == [(True, [("delete", f"designates:{AUTHOR}")])]


def test_self_designation_is_ignored(monkeypatch):
    session, log = _ingest(monkeypatch, _event([["30382:rank", AUTHOR]]))

    assert session.run.await_args.kwargs["designated"] == []
    assert log == [(True, [("delete", f"designates:{AUTHOR}")])]


def test_backfill_replays_only_each_authors_newest_designation():
    from scripts.backfill_designations import latest_per_author

    older = {**_event([["30382:rank", OTHER_PROVIDER]]), "created_at": 1}
    newer = {**_event([["30382:rank", PROVIDER]]), "created_at": 2}
    other_author = {**_event([]), "pubkey": "e" * 64, "created_at": 1}
    not_a_designation = {**newer, "kind": 3, "created_at": 3}

    assert latest_per_author([newer, older, other_author, not_a_designation]) == [
        newer,
        other_author,
    ]
