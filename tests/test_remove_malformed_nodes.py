"""Removing malformed-pubkey nodes: which edges a replay can fix, and that the
list ingest applies last is the relay's current one."""
import asyncio
import json

import pytest

from scripts import remove_malformed_nodes as mod

UPPER = "A" * 64
FOLLOWER = "b" * 64
OTHER = "c" * 64


def _list(author: str, created_at: int, event_id: str) -> dict:
    return {
        "kind": 3,
        "pubkey": author,
        "created_at": created_at,
        "id": event_id,
        "tags": [],
    }


def test_followers_of_a_malformed_node_are_the_authors_to_replay():
    plan = mod.plan_for(
        [
            mod.Edge(node=UPPER, other=FOLLOWER, type="FOLLOWS", outgoing=False),
            mod.Edge(node=UPPER, other=OTHER, type="FOLLOWS", outgoing=False),
            mod.Edge(node="D" * 64, other=FOLLOWER, type="FOLLOWS", outgoing=False),
        ]
    )
    assert plan.authors == [FOLLOWER, OTHER]
    assert plan.unfixable == []


@pytest.mark.parametrize(
    "edge",
    [
        mod.Edge(node=UPPER, other=FOLLOWER, type="FOLLOWS", outgoing=True),
        mod.Edge(node=UPPER, other=FOLLOWER, type="REPORTS", outgoing=False),
        mod.Edge(node=UPPER, other="E" * 64, type="FOLLOWS", outgoing=False),
    ],
)
def test_an_edge_a_follow_list_cannot_rewrite_is_not_planned(edge):
    plan = mod.plan_for([edge])
    assert plan.authors == []
    assert [e for e, _ in plan.unfixable] == [edge]


def test_the_newest_list_of_each_author_is_kept():
    lists = mod.newest_lists(
        [
            _list(FOLLOWER, 10, "old"),
            _list(FOLLOWER, 20, "new"),
            _list(OTHER, 5, "only"),
            {"kind": 10000, "pubkey": OTHER, "created_at": 99, "id": "mutes"},
        ]
    )
    assert {a: e["id"] for a, e in lists.items()} == {FOLLOWER: "new", OTHER: "only"}


class _Queue:
    def __init__(self):
        self.pushed: list[str] = []

    async def rpush(self, name, value):
        assert name == mod.STRFRY_EVENTS_QUEUE_NAME
        self.pushed.append(json.loads(value)["id"])


def test_a_list_replaced_during_the_replay_is_queued_after_the_one_it_replaces(
    monkeypatch,
):
    first = {FOLLOWER: _list(FOLLOWER, 10, "first"), OTHER: _list(OTHER, 10, "other")}
    answers = [
        {FOLLOWER: _list(FOLLOWER, 20, "second"), OTHER: first[OTHER]},
        {FOLLOWER: _list(FOLLOWER, 20, "second"), OTHER: first[OTHER]},
    ]

    async def fetch(authors):
        return answers.pop(0)

    monkeypatch.setattr(mod, "fetch_follow_lists", fetch)
    queue = _Queue()
    asyncio.run(mod.replay(queue, [FOLLOWER, OTHER], first))
    assert queue.pushed == ["first", "other", "second"]


def test_lists_that_never_settle_stop_the_replay(monkeypatch):
    n = 0

    async def fetch(authors):
        nonlocal n
        n += 1
        return {FOLLOWER: _list(FOLLOWER, n, f"v{n}")}

    monkeypatch.setattr(mod, "fetch_follow_lists", fetch)
    with pytest.raises(RuntimeError):
        asyncio.run(
            mod.replay(_Queue(), [FOLLOWER], {FOLLOWER: _list(FOLLOWER, 0, "v0")})
        )
