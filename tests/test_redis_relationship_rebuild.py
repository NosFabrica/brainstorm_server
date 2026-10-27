"""The one-shot Redis rebuild from Neo4j covers the forward sets too.

`designates:` (kind-10040) and `zaps_via:` (written by brainstorm_profile_resolver) are what
the GrapeRank worker reads for designations; Neo4j holds the same edges. If Redis is wiped,
nothing else restores them — the resolver skips users it already settled — so the rebuild
writes them from `DESIGNATES` / `ZAPS_VIA` edges, keyed by source, beside the reverse sets.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.message_queue_tasks import backfill_redis_relationships as mod

ALICE = "a" * 64
BOB = "b" * 64
PROVIDER = "c" * 64


class _Pipe:
    def __init__(self, writes: list):
        self.writes = writes
        self.pending: list = []

    def sadd(self, key, member):
        self.pending.append((key, member))

    async def execute(self):
        self.writes.extend(self.pending)
        self.pending = []


class _Result:
    def __init__(self, records):
        self.records = records

    def __aiter__(self):
        async def gen():
            for r in self.records:
                yield r

        return gen()


def _driver(pages: list[list[dict]]):
    """A Neo4j driver whose session answers the page query with `pages`, then empty."""
    calls = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, cypher, **params):
            calls.append((cypher, params))
            index = len(calls) - 1
            return _Result(pages[index] if index < len(pages) else [])

    driver = MagicMock()
    driver.session = lambda: _Session()
    return driver, calls


def _rebuild(monkeypatch, relationship, prefix, forward, pages):
    driver, calls = _driver(pages)
    monkeypatch.setattr(mod, "neo4j_driver", driver)
    writes: list = []
    redis = MagicMock()
    redis.pipeline = lambda transaction: _Pipe(writes)
    asyncio.run(
        mod._backfill_relationship(redis, relationship, prefix, 2, forward=forward)
    )
    return writes, calls


def test_forward_sets_are_keyed_by_the_source(monkeypatch):
    pages = [[{"source": ALICE, "targets": [PROVIDER]}, {"source": BOB, "targets": []}]]
    writes, calls = _rebuild(monkeypatch, "DESIGNATES", "designates:", True, pages)

    assert writes == [(f"designates:{ALICE}", PROVIDER)]
    assert "[:DESIGNATES]" in calls[0][0]


def test_reverse_sets_are_keyed_by_the_target(monkeypatch):
    pages = [[{"source": ALICE, "targets": [BOB]}]]
    writes, _ = _rebuild(monkeypatch, "FOLLOWS", "followed_by:", False, pages)

    assert writes == [(f"followed_by:{BOB}", ALICE)]


def test_the_rebuild_covers_designations_and_zap_signers(monkeypatch):
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=False)
    redis.set = AsyncMock()
    redis.close = AsyncMock()
    monkeypatch.setattr(mod, "get_redis_client", lambda: redis)
    monkeypatch.setattr(mod, "_is_graph_db_populated", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mod, "_redis_has_relationship_data", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(mod, "_count_nostr_users", AsyncMock(return_value=1))
    rebuilt = AsyncMock(return_value=0)
    monkeypatch.setattr(mod, "_backfill_relationship", rebuilt)

    asyncio.run(mod.backfill_redis_relationships_if_needed())

    asked = [
        (c.args[1], c.args[2], c.kwargs.get("forward", False))
        for c in rebuilt.await_args_list
    ]
    assert ("DESIGNATES", "designates:", True) in asked
    assert ("ZAPS_VIA", "zaps_via:", True) in asked
    assert ("FOLLOWS", "followed_by:", False) in asked
    redis.set.assert_awaited_once_with(mod.DONE_MARKER_KEY, "1")


def test_left_over_forward_keys_count_as_partial_state(monkeypatch):
    async def scan_iter(match, count):
        if match == "zaps_via:*":
            yield f"zaps_via:{ALICE}"

    redis = MagicMock()
    redis.scan_iter = scan_iter
    assert asyncio.run(mod._redis_has_relationship_data(redis)) is True
