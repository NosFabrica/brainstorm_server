"""kind-30000 vouch-list ingestion: an author's d="vouches" follow set becomes
`VOUCHES` edges and the `vouched_by:<pubkey>` reverse sets GrapeRank reads.
Any other d-value is an ordinary NIP-51 follow set and is ignored.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.message_queue_tasks import process_strfry_event as mod
from app.message_queue_tasks.process_strfry_event import process_strfry_event

AUTHOR = "a" * 64
VOUCHED = "b" * 64
ALSO_VOUCHED = "c" * 64


def _event(tags: list, kind: int = 30000) -> dict:
    return {"kind": kind, "pubkey": AUTHOR, "tags": tags, "content": ""}


def _mock_session(removed: list | None = None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.single = AsyncMock(return_value={"removed": removed or []})
    session.run = AsyncMock(return_value=result)

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

    def sadd(self, key, *members):
        self.ops.append(("sadd", key, *members))

    def srem(self, key, *members):
        self.ops.append(("srem", key, *members))

    async def execute(self):
        self.log.append((self.transaction, self.ops))


def _ingest(monkeypatch, event: dict, removed: list | None = None):
    log: list = []
    redis = MagicMock()
    redis.pipeline = lambda transaction: _Pipe(log, transaction)
    monkeypatch.setattr(mod, "redis_client", redis)
    session = _mock_session(removed)
    asyncio.run(process_strfry_event(session, event))
    return session, log


def test_vouch_list_becomes_edges_and_reverse_sets(monkeypatch):
    session, log = _ingest(
        monkeypatch,
        _event([["d", "vouches"], ["p", VOUCHED], ["p", ALSO_VOUCHED]]),
    )

    # Upsert, then cleanup of dropped vouches.
    assert session.run.await_count == 2
    upsert = session.run.await_args_list[0]
    assert "MERGE (pub)-[:VOUCHES]->(f)" in upsert.args[0]
    assert upsert.kwargs == {
        "publisher": AUTHOR,
        "vouched_pubkeys": [VOUCHED, ALSO_VOUCHED],
    }

    ops = [op for _tx, ops_ in log for op in ops_]
    assert ("sadd", f"vouched_by:{VOUCHED}", AUTHOR) in ops
    assert ("sadd", f"vouched_by:{ALSO_VOUCHED}", AUTHOR) in ops


def test_replacement_removes_dropped_vouches(monkeypatch):
    session, log = _ingest(
        monkeypatch,
        _event([["d", "vouches"], ["p", VOUCHED]]),
        removed=[ALSO_VOUCHED],
    )

    cleanup = session.run.await_args_list[1]
    assert "DELETE rel" in cleanup.args[0]

    ops = [op for _tx, ops_ in log for op in ops_]
    assert ("sadd", f"vouched_by:{VOUCHED}", AUTHOR) in ops
    assert ("srem", f"vouched_by:{ALSO_VOUCHED}", AUTHOR) in ops


def test_empty_vouch_list_clears_the_authors_vouches(monkeypatch):
    session, log = _ingest(monkeypatch, _event([["d", "vouches"]]), removed=[VOUCHED])

    session.run.assert_awaited_once()
    assert "DELETE rel" in session.run.await_args.args[0]

    ops = [op for _tx, ops_ in log for op in ops_]
    assert ops == [("srem", f"vouched_by:{VOUCHED}", AUTHOR)]


def test_other_follow_sets_are_ignored(monkeypatch):
    session, log = _ingest(
        monkeypatch, _event([["d", "interesting-people"], ["p", VOUCHED]])
    )

    session.run.assert_not_called()
    assert log == []


def test_a_missing_d_tag_is_ignored(monkeypatch):
    session, log = _ingest(monkeypatch, _event([["p", VOUCHED]]))

    session.run.assert_not_called()
    assert log == []


def test_bookmark_sets_are_ignored_even_with_the_vouch_d_tag(monkeypatch):
    session, log = _ingest(
        monkeypatch,
        _event([["d", "vouches"], ["p", VOUCHED]], kind=30003),
    )

    session.run.assert_not_called()
    assert log == []
