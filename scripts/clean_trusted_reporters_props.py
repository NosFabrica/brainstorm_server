"""Remove leftover `trusted_reporters_<observer>` properties from NostrUser nodes.

The reporter count is computed live, so these stored props are inert; removing
them only frees space. Observer keys are discovered from the database's
property keys. Influence, hops and trusted-follower/muter props are untouched.

DRY-RUN by default (prints property + node counts, writes nothing). Pass --apply
to remove them in bounded batches (one transaction per batch — a single large
transaction exhausts Neo4j's transaction memory). Idempotent / re-runnable.

    poetry run python -m scripts.clean_trusted_reporters_props [--apply] [--batch N]

Freed records are reused, but store files only shrink after a dump and reload,
and property-key tokens are never reclaimed (so keys stay discoverable at 0).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402

KEY_PREFIX = "trusted_reporters_"


@dataclass(frozen=True)
class RemoveResult:
    nodes: int
    batches: int


async def discover_keys(session) -> list[str]:
    res = await session.run(
        "CALL db.propertyKeys() YIELD propertyKey "
        "WHERE propertyKey STARTS WITH $prefix RETURN propertyKey",
        prefix=KEY_PREFIX,
    )
    return sorted([r["propertyKey"] async for r in res])


async def count_props(session, keys: list[str]) -> tuple[int, int]:
    """(properties, nodes) holding any of `keys`."""
    # Map lookup over keys(u): ~200x faster than probing u[k] for every key.
    res = await session.run(
        "MATCH (u:NostrUser) "
        "WITH size([k IN keys(u) WHERE $keyset[k] IS NOT NULL]) AS n WHERE n > 0 "
        "RETURN coalesce(sum(n), 0) AS props, count(*) AS nodes",
        keyset=dict.fromkeys(keys, True),
    )
    row = await res.single()
    return row["props"], row["nodes"]


async def remove_props(session, keys: list[str], batch: int) -> RemoveResult:
    # Setting a prop to null via += removes it. Serial batches: Neo4j fails under concurrent writers.
    nulls = {k: None for k in keys}
    nodes = batches = 0
    while True:
        res = await session.run(
            "MATCH (u:NostrUser) WHERE any(k IN keys(u) WHERE $keyset[k] IS NOT NULL) "
            "WITH u LIMIT $batch SET u += $nulls RETURN count(u) AS n",
            keyset=dict.fromkeys(keys, True),
            batch=batch,
            nulls=nulls,
        )
        n = (await res.single())["n"]
        if n == 0:
            return RemoveResult(nodes, batches)
        nodes += n
        batches += 1
        print(f"  batch {batches}: {n} nodes ({nodes} total)", file=sys.stderr)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="remove (default: dry-run)")
    ap.add_argument("--batch", type=int, default=10000, help="nodes per transaction")
    args = ap.parse_args()

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    t0 = time.monotonic()
    async with neo4j_driver.session() as session:
        keys = await discover_keys(session)
        props, nodes = await count_props(session, keys)
        log(f"observer keys: {len(keys)}")
        log(f"leftover: {props} properties on {nodes} nodes")

        if not args.apply:
            log(f"[dry-run] no writes. elapsed {time.monotonic() - t0:.2f}s")
            return

        result = await remove_props(session, keys, args.batch)
        log(
            f"[applied] cleared {result.nodes} nodes in {result.batches} batches. "
            f"elapsed {time.monotonic() - t0:.2f}s"
        )


if __name__ == "__main__":
    asyncio.run(main())
