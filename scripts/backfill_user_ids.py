"""Give every existing NostrUser its `user_id`, and check that none is without one.

Dry-run by default: prints what is missing. See scripts/CLAUDE.md.

    poetry run python -m scripts.backfill_user_ids [--apply] [--page N] [--batch N]
    poetry run python -m scripts.backfill_user_ids --status
    poetry run python -m scripts.backfill_user_ids --verify
    poetry run python -m scripts.backfill_user_ids --reconcile [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from app.core.config import settings  # noqa: E402
from app.core.scorecard_database import (  # noqa: E402
    dispose_scorecard_engine,
    unbounded_statements,
)
from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402
from app.neo4j_db.schema import ensure_schema  # noqa: E402
from app.repos.user_id_repo import (  # noqa: E402
    all_user_ids,
    count_users,
    ids_for,
    restore_users,
)

HEX64 = "[0-9a-f]{64}"
_WELL_FORMED = re.compile(HEX64)

_COUNTS = f"""
MATCH (u:NostrUser)
RETURN count(u) AS total,
       count(u.user_id) AS with_id,
       sum(CASE WHEN u.pubkey IS NULL OR NOT u.pubkey =~ '{HEX64}' THEN 1 ELSE 0 END)
           AS malformed
"""

# Keyset pages in pubkey order, off the pubkey index.
_PAGE_WITHOUT_ID = f"""
MATCH (u:NostrUser)
WHERE u.pubkey > $after AND u.pubkey =~ '{HEX64}' AND u.user_id IS NULL
RETURN u.pubkey AS pubkey ORDER BY u.pubkey LIMIT $limit
"""

_PAGE_ALL = """
MATCH (u:NostrUser) WHERE u.pubkey > $after
RETURN u.pubkey AS pubkey, u.user_id AS user_id ORDER BY u.pubkey LIMIT $limit
"""

_SET_IDS = """
UNWIND $rows AS row
MATCH (u:NostrUser {pubkey: row.pubkey}) WHERE u.user_id IS NULL
SET u.user_id = row.user_id
RETURN count(u) AS n
"""


@dataclass(frozen=True)
class Status:
    nodes: int
    with_id: int
    malformed: int
    table_rows: int
    ids_without_node: int

    @property
    def without_id(self) -> int:
        return self.nodes - self.with_id


@dataclass
class Verdict:
    nodes: int = 0
    without_id: int = 0  # well-formed pubkey, no id
    malformed: int = 0  # can never have one; reported, does not fail
    disagree: list[tuple[str, int, int | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.without_id and not self.disagree


@dataclass
class Plan:
    """What --reconcile would put back into `nostr_user`."""

    to_insert: list[tuple[int, str]] = field(default_factory=list)
    conflicts: list[tuple[str, int, int]] = field(default_factory=list)
    highest_id: int = 0


async def _pages(session, cypher: str, limit: int) -> AsyncIterator[list[dict]]:
    after = ""
    while True:
        result = await session.run(cypher, after=after, limit=limit)
        rows = [dict(r) async for r in result]
        if not rows:
            return
        yield rows
        after = rows[-1]["pubkey"]


async def counts(session) -> tuple[int, int, int]:
    record = await (await session.run(_COUNTS)).single()
    return record["total"], record["with_id"], record["malformed"]


async def status(session) -> Status:
    nodes, with_id, malformed = await counts(session)
    result = await session.run(
        "MATCH (u:NostrUser) WHERE u.user_id IS NOT NULL RETURN u.user_id AS i"
    )
    graph_ids = {r["i"] async for r in result}
    return Status(
        nodes=nodes,
        with_id=with_id,
        malformed=malformed,
        table_rows=await count_users(),
        ids_without_node=len(await all_user_ids() - graph_ids),
    )


async def backfill(session, page: int, batch: int, log=print) -> int:
    """Set `user_id` on every well-formed node without one. Returns nodes written."""
    await ensure_schema(session, user_ids=True)
    written = 0
    t0 = time.monotonic()
    async for rows in _pages(session, _PAGE_WITHOUT_ID, page):
        ids = await ids_for([r["pubkey"] for r in rows])
        pairs = [{"pubkey": pk, "user_id": uid} for pk, uid in ids.items()]
        for i in range(0, len(pairs), batch):
            chunk = pairs[i : i + batch]

            async def work(tx, chunk=chunk):
                return (await (await tx.run(_SET_IDS, rows=chunk)).single())["n"]

            written += await session.execute_write(work)
        rate = written / max(time.monotonic() - t0, 1e-9)
        log(
            f"  {written} nodes given ids, {rate:.0f}/s, up to {rows[-1]['pubkey'][:12]}…"
        )
    return written


async def verify(session, page: int) -> Verdict:
    verdict = Verdict()
    async for rows in _pages(session, _PAGE_ALL, page):
        verdict.nodes += len(rows)
        with_id = [r for r in rows if r["user_id"] is not None]
        for r in rows:
            if r["user_id"] is None:
                if _WELL_FORMED.fullmatch(r["pubkey"]):
                    verdict.without_id += 1
                else:
                    verdict.malformed += 1
        checkable = [
            r["pubkey"] for r in with_id if _WELL_FORMED.fullmatch(r["pubkey"])
        ]
        table = await ids_for(checkable, create=False)
        for r in with_id:
            if table.get(r["pubkey"]) != r["user_id"]:
                verdict.disagree.append(
                    (r["pubkey"], r["user_id"], table.get(r["pubkey"]))
                )
    return verdict


async def plan_reconcile(session, page: int) -> Plan:
    plan = Plan()
    taken = await all_user_ids()
    async for rows in _pages(session, _PAGE_ALL, page):
        with_id = [
            r
            for r in rows
            if r["user_id"] is not None and _WELL_FORMED.fullmatch(r["pubkey"])
        ]
        table = await ids_for([r["pubkey"] for r in with_id], create=False)
        for r in with_id:
            pubkey, graph_id = r["pubkey"], r["user_id"]
            plan.highest_id = max(plan.highest_id, graph_id)
            if pubkey in table:
                if table[pubkey] != graph_id:
                    plan.conflicts.append((pubkey, graph_id, table[pubkey]))
            elif graph_id in taken:
                # The table gave this id to another pubkey since the restore.
                plan.conflicts.append((pubkey, graph_id, -1))
            else:
                plan.to_insert.append((graph_id, pubkey))
    return plan


def _print_status(s: Status) -> None:
    print(f"nodes: {s.nodes}")
    print(f"  with user_id: {s.with_id}")
    print(f"  without user_id: {s.without_id} (malformed pubkey: {s.malformed})")
    print(f"nostr_user rows: {s.table_rows}")
    print(f"  ids with no node: {s.ids_without_node}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--page", type=int, default=10_000, help="pubkeys per read")
    ap.add_argument(
        "--batch",
        type=int,
        default=settings.neo4j_write_batch_size,
        help="nodes per write transaction",
    )
    args = ap.parse_args()
    unbounded_statements()

    t0 = time.monotonic()
    try:
        async with neo4j_driver.session() as session:
            if args.status:
                _print_status(await status(session))
                return 0

            if args.verify:
                verdict = await verify(session, args.page)
                print(f"nodes: {verdict.nodes}")
                print(f"without user_id: {verdict.without_id}")
                print(f"malformed pubkey, cannot have one: {verdict.malformed}")
                print(f"disagreeing with nostr_user: {len(verdict.disagree)}")
                for pubkey, graph_id, table_id in verdict.disagree[:20]:
                    print(f"  {pubkey}: graph {graph_id}, table {table_id}")
                return 0 if verdict.ok else 1

            if args.reconcile:
                plan = await plan_reconcile(session, args.page)
                print(f"rows the table lacks: {len(plan.to_insert)}")
                print(f"highest user_id in the graph: {plan.highest_id}")
                print(f"conflicts: {len(plan.conflicts)}")
                for pubkey, graph_id, table_id in plan.conflicts[:20]:
                    other = "another pubkey" if table_id == -1 else table_id
                    print(f"  {pubkey}: graph {graph_id}, table {other}")
                if plan.conflicts:
                    print("stopped: resolve the conflicts first; nothing written")
                    return 2
                if not args.apply:
                    print("[dry-run] no writes")
                    return 0
                inserted = await restore_users(plan.to_insert, plan.highest_id)
                print(f"[applied] {inserted} rows restored, sequence moved")
                return 0

            nodes, with_id, malformed = await counts(session)
            print(f"nodes: {nodes}, without user_id: {nodes - with_id}")
            if malformed:
                print(f"  {malformed} have a malformed pubkey and are skipped")
            if not args.apply:
                print(f"[dry-run] no writes. elapsed {time.monotonic() - t0:.2f}s")
                return 0
            written = await backfill(session, args.page, args.batch)
            print(
                f"[applied] {written} nodes given ids. "
                f"elapsed {time.monotonic() - t0:.2f}s"
            )
            return 0
    finally:
        await dispose_scorecard_engine()
        await neo4j_driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
