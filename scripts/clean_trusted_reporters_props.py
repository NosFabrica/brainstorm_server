"""Remove leftover `trusted_reporters_<observer>` props from NostrUser nodes.

Dry-run by default; --apply removes them in batches. See scripts/CLAUDE.md.

    poetry run python -m scripts.clean_trusted_reporters_props [--apply] [--batch N]
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
    # Chunk by node id, one small transaction each. A single MATCH ... SET
    # plans an Eager that holds every match in one transaction and OOMs at scale.
    res = await session.run("MATCH (u:NostrUser) RETURN max(id(u)) AS m")
    max_id = (await res.single())["m"]
    if max_id is None:
        return RemoveResult(0, 0)
    nodes = batches = 0
    for lo in range(0, max_id + 1, batch):
        res = await session.run(
            "UNWIND range($lo, $hi) AS i MATCH (u:NostrUser) WHERE id(u) = i "
            "AND any(k IN keys(u) WHERE $keyset[k] IS NOT NULL) "
            "SET u += $nulls RETURN count(u) AS n",
            lo=lo,
            hi=min(lo + batch - 1, max_id),
            keyset=dict.fromkeys(keys, True),
            nulls={k: None for k in keys},
        )
        n = (await res.single())["n"]
        if n:
            nodes += n
            batches += 1
        if (lo // batch) % 100 == 0:
            print(f"  ids {lo}/{max_id}: {nodes} nodes cleared")
    return RemoveResult(nodes, batches)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="remove (default: dry-run)")
    ap.add_argument("--batch", type=int, default=1000, help="node ids per transaction")
    args = ap.parse_args()

    t0 = time.monotonic()
    # remove_props seeks by id(), deprecated but fine on 5.x; keep the output readable.
    async with neo4j_driver.session(
        notifications_disabled_classifications=["DEPRECATION"]
    ) as session:
        keys = await discover_keys(session)
        props, nodes = await count_props(session, keys)
        print(f"observer keys: {len(keys)}")
        print(f"leftover: {props} properties on {nodes} nodes")

        if not args.apply:
            print(f"[dry-run] no writes. elapsed {time.monotonic() - t0:.2f}s")
            return

        result = await remove_props(session, keys, args.batch)
        print(
            f"[applied] cleared {result.nodes} nodes in {result.batches} batches. "
            f"elapsed {time.monotonic() - t0:.2f}s"
        )


if __name__ == "__main__":
    asyncio.run(main())
