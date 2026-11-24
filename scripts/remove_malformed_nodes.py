"""Remove the NostrUser nodes whose pubkey is not 64-char lowercase hex.

Such a node can never have a `user_id`. Those still followed come from `p` tags
written before ingest lowercased them: each follower's current kind 3 is read
from the relay and put on the ingest queue, which rewrites the follows onto the
lowercase node. A node is deleted once nothing points at it.

Dry-run by default. See scripts/CLAUDE.md.

    poetry run python -m scripts.remove_malformed_nodes [--apply] [--wait SECONDS]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from app.core.config import settings  # noqa: E402
from app.core.redis_db import get_redis_client  # noqa: E402
from app.message_queue_tasks.ingest_gate import may_ingest  # noqa: E402
from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402

HEX64 = "[0-9a-f]{64}"
_WELL_FORMED = re.compile(HEX64)
_MALFORMED = f"(n.pubkey IS NULL OR NOT n.pubkey =~ '{HEX64}')"

STRFRY_EVENTS_QUEUE_NAME = "strfry:events"
FOLLOW_LIST_KIND = 3
_RELAY_TIMEOUT_SECONDS = 60.0
# A list published while this runs is put on the queue again; this many times.
_SETTLE_ROUNDS = 5

_NODES = f"""
MATCH (n:NostrUser) WHERE {_MALFORMED}
RETURN elementId(n) AS id, n.pubkey AS pubkey
"""

_EDGES = """
MATCH (n:NostrUser) WHERE elementId(n) IN $ids
MATCH (n)-[r]-(o)
RETURN n.pubkey AS node, o.pubkey AS other, type(r) AS type, startNode(r) = n AS outgoing
"""

_EDGE_COUNT = """
MATCH (n:NostrUser) WHERE elementId(n) IN $ids
RETURN sum(COUNT { (n)--() }) AS edges
"""

_DELETE_EDGE_FREE = """
MATCH (n:NostrUser) WHERE elementId(n) IN $ids AND NOT (n)--()
DELETE n
RETURN count(*) AS deleted
"""


@dataclass(frozen=True)
class Edge:
    node: str | None
    other: str | None
    type: str
    outgoing: bool


@dataclass
class Plan:
    authors: list[str] = field(default_factory=list)
    # Edges a follow list cannot rewrite, each with the reason.
    unfixable: list[tuple[Edge, str]] = field(default_factory=list)


def plan_for(edges: list[Edge]) -> Plan:
    plan = Plan()
    authors: dict[str, None] = {}
    for edge in edges:
        if edge.outgoing:
            plan.unfixable.append((edge, "the malformed node is the author"))
        elif edge.type != "FOLLOWS":
            plan.unfixable.append((edge, "not a follow"))
        elif not edge.other or not _WELL_FORMED.fullmatch(edge.other):
            plan.unfixable.append((edge, "the follower is malformed too"))
        else:
            authors[edge.other] = None
    plan.authors = sorted(authors)
    return plan


def newest_lists(events: list[dict]) -> dict[str, dict]:
    """Each author's newest kind 3 among `events`."""
    newest: dict[str, dict] = {}
    for event in events:
        if event.get("kind") != FOLLOW_LIST_KIND:
            continue
        current = newest.get(event["pubkey"])
        if current is None or event["created_at"] >= current["created_at"]:
            newest[event["pubkey"]] = event
    return newest


async def fetch_follow_lists(authors: list[str]) -> dict[str, dict]:
    """The relay's current kind 3 of each author. Raises unless the relay
    answered in full (EOSE): a list missed here would stay unreplayed."""
    sub_id = "remove-malformed"
    events: list[dict] = []
    async with asyncio.timeout(_RELAY_TIMEOUT_SECONDS):
        async with websockets.connect(
            settings.nostr_transfer_to_relay, max_size=None
        ) as ws:
            await ws.send(
                json.dumps(
                    ["REQ", sub_id, {"kinds": [FOLLOW_LIST_KIND], "authors": authors}]
                )
            )
            while True:
                msg = json.loads(await ws.recv())
                if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub_id:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3:
                    events.append(msg[2])
                elif msg[0] == "EOSE":
                    break
                elif msg[0] == "CLOSED":
                    raise RuntimeError(f"the relay closed the read: {msg[2:]}")
    return newest_lists(events)


async def replay(redis_client, authors: list[str], lists: dict[str, dict]) -> None:
    """Queue each list, then again any that the relay has since replaced: the
    last one ingest applies for an author must be the relay's current one."""
    queued = {author: lists[author] for author in authors}
    to_push = list(queued.values())
    for _ in range(_SETTLE_ROUNDS):
        for event in to_push:
            await redis_client.rpush(STRFRY_EVENTS_QUEUE_NAME, json.dumps(event))
        current = await fetch_follow_lists(authors)
        to_push = [
            event
            for author, event in current.items()
            if event["id"] != queued[author]["id"]
        ]
        if not to_push:
            return
        queued.update({event["pubkey"]: event for event in to_push})
    raise RuntimeError("follow lists kept changing; run again")


async def _one(session, query: str, ids: list[str]) -> int:
    record = await (await session.run(query, ids=ids)).single(strict=True)
    return record[0]


async def wait_for_edges_gone(session, ids: list[str], seconds: int) -> int:
    deadline = time.monotonic() + seconds
    while True:
        edges = await _one(session, _EDGE_COUNT, ids)
        if not edges or time.monotonic() >= deadline:
            return edges
        print(f"  {edges} edges left")
        await asyncio.sleep(2)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument(
        "--wait",
        type=int,
        default=300,
        help="seconds to wait for ingest to apply the lists",
    )
    args = ap.parse_args()

    redis_client = get_redis_client()
    try:
        async with neo4j_driver.session() as session:
            nodes = [r.data() async for r in await session.run(_NODES)]
            print(f"malformed nodes: {len(nodes)}")
            if not nodes:
                return 0
            ids = [node["id"] for node in nodes]
            edges = [Edge(**r.data()) async for r in await session.run(_EDGES, ids=ids)]
            plan = plan_for(edges)
            print(f"edges on them: {len(edges)}, from {len(plan.authors)} followers")
            for edge, reason in plan.unfixable[:20]:
                print(f"  cannot fix {edge.type} {edge.other} / {edge.node}: {reason}")
            if plan.unfixable:
                print("stopped: these edges need a decision; nothing written")
                return 2

            lists = await fetch_follow_lists(plan.authors) if plan.authors else {}
            missing = [author for author in plan.authors if author not in lists]
            print(f"follow lists on the relay: {len(lists)} of {len(plan.authors)}")
            for author in missing:
                print(f"  no kind 3 on the relay: {author}")
            if missing:
                print("stopped: their follows cannot be replayed; nothing written")
                return 2

            if not args.apply:
                print("[dry-run] no writes")
                return 0

            if plan.authors:
                if not await may_ingest(redis_client):
                    print("stopped: ingest is held, the lists would wait in the queue")
                    return 2
                await replay(redis_client, plan.authors, lists)
                print(f"queued {len(plan.authors)} follow lists for ingest")
                left = await wait_for_edges_gone(session, ids, args.wait)
                if left:
                    print(f"{left} edges still on malformed nodes after {args.wait}s")

            deleted = await _one(session, _DELETE_EDGE_FREE, ids)
            print(f"[applied] deleted {deleted} of {len(nodes)} malformed nodes")
            return 0 if deleted == len(nodes) else 1
    finally:
        await redis_client.close()
        await neo4j_driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
