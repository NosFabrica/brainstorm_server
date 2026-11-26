"""Switching the Redis relationship sets between pubkeys and User ids at a server
start: which sets are renamed from, and that stale ones never are."""
from __future__ import annotations

import asyncio
import fnmatch

import pytest

from app.message_queue_tasks import backfill_redis_relationships as mod

PK_A, PK_B = "a" * 64, "b" * 64
IDS = {PK_A: "1", PK_B: "2"}


class _Pipe:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def smembers(self, key):
        self.ops.append(("smembers", key))

    def sadd(self, key, *members):
        self.ops.append(("sadd", key, members))

    async def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "smembers":
                out.append(set(self.redis.data.get(op[1], set())))
            else:
                self.redis.data.setdefault(op[1], set()).update(op[2])
                out.append(len(op[2]))
        self.ops = []
        return out


class _Redis:
    def __init__(self, data=None):
        self.data: dict = dict(data or {})

    async def exists(self, key):
        return int(key in self.data)

    async def set(self, key, value):
        self.data[key] = value

    async def mset(self, mapping):
        self.data.update(mapping)

    async def delete(self, *keys):
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def unlink(self, *keys):
        return await self.delete(*keys)

    async def scan_iter(self, match, count=None):
        for key in sorted(self.data):
            if fnmatch.fnmatchcase(key, match):
                yield key

    def pipeline(self, transaction=False):
        return _Pipe(self)


@pytest.fixture
def switch(monkeypatch):
    """Runs the id-naming start against a fake Redis; Neo4j rebuilds are recorded."""
    rebuilt: list[bool] = []

    async def ids_for(pubkeys, create=True):
        return {pk: int(IDS[pk]) for pk in pubkeys if pk in IDS}

    async def pubkeys_for(ids):
        back = {int(v): k for k, v in IDS.items()}
        return {i: back[i] for i in ids if i in back}

    async def rebuild(redis, ids):
        rebuilt.append(ids)
        redis.data["followed_by:1" if ids else f"followed_by:{PK_A}"] = {"from-neo4j"}

    async def populated():
        return True

    monkeypatch.setattr(mod, "ids_for", ids_for)
    monkeypatch.setattr(mod, "pubkeys_for", pubkeys_for)
    monkeypatch.setattr(mod, "_rebuild_sets", rebuild)
    monkeypatch.setattr(mod, "_is_graph_db_populated", populated)

    def run(redis, step=mod._build_id_sets_if_needed):
        asyncio.run(step(redis))
        return rebuilt

    return run


def _sets(redis):
    return {k: v for k, v in redis.data.items() if not k.startswith("migration:")}


def test_the_first_switch_renames_the_pubkey_sets_and_removes_them(switch):
    redis = _Redis({f"followed_by:{PK_A}": {PK_B}, mod.DONE_MARKER_KEY: "1"})
    assert switch(redis) == []
    assert _sets(redis) == {"followed_by:1": {"2"}}
    assert redis.data[mod.IDS_DONE_MARKER_KEY] == "1"
    assert redis.data[mod.PUBKEYS_STALE_KEY] == "1"


def test_a_switch_cut_short_starts_again_from_the_pubkey_sets(switch):
    """Nothing ingests by id before the switch has finished once."""
    redis = _Redis({f"followed_by:{PK_A}": {PK_B}, "followed_by:1": {"9"}})
    switch(redis)
    assert _sets(redis) == {"followed_by:1": {"2"}}


def test_a_lost_marker_keeps_the_id_sets_and_never_reads_stale_pubkey_sets(switch):
    """The id sets took a new follower since the switch; the pubkey set did not."""
    redis = _Redis(
        {
            "followed_by:1": {"2", "7"},
            f"followed_by:{PK_A}": {PK_B},
            mod.PUBKEYS_STALE_KEY: "1",
        }
    )
    assert switch(redis) == []
    assert _sets(redis) == {"followed_by:1": {"2", "7"}}
    assert redis.data[mod.IDS_DONE_MARKER_KEY] == "1"


def test_with_the_marker_present_nothing_is_touched(switch):
    before = {"followed_by:1": {"2"}, mod.IDS_DONE_MARKER_KEY: "1"}
    redis = _Redis(before)
    switch(redis)
    assert redis.data == before


def test_a_wiped_redis_is_built_from_neo4j(switch):
    redis = _Redis()
    assert switch(redis) == [True]
    assert redis.data[mod.IDS_DONE_MARKER_KEY] == "1"


def test_id_sets_gone_but_once_complete_are_built_from_neo4j_not_stale_pubkeys(switch):
    redis = _Redis({f"followed_by:{PK_A}": {PK_B}, mod.PUBKEYS_STALE_KEY: "1"})
    assert switch(redis) == [True]
    assert _sets(redis) == {"followed_by:1": {"from-neo4j"}}


def test_back_to_pubkeys_renames_the_id_sets_even_without_their_marker(switch):
    redis = _Redis({"followed_by:1": {"2"}, mod.PUBKEYS_STALE_KEY: "1"})
    assert switch(redis, mod._rebuild_pubkey_sets_after_ids) == []
    assert redis.data[f"followed_by:{PK_A}"] == {PK_B}
    assert mod.PUBKEYS_STALE_KEY not in redis.data
    assert redis.data[mod.DONE_MARKER_KEY] == "1"
