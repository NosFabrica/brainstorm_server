"""Backfill: recompute the user-only report graph from the relay.

Rebuilds Neo4j `REPORTS` + Redis `reported_by:` from the relay's kind-1984
reports, minus same-author `k=1984` kind-5 deletions. Only user-level reports
count — note/media reports (any `e` tag) are dropped; a report is dropped if a
same-author kind-5 carrying a `k=1984` tag references its id. Deleted reports
that the relay already purged are simply absent from the scan.

DRY-RUN by default (prints the delta + counts + elapsed, writes nothing).
Pass --apply to write. Safe to run live: Neo4j is a full chunked rebuild
(property-less edges), Redis is a member-level SADD/SREM diff (no whole-key DEL,
so a concurrent ingest is not clobbered). Idempotent / re-runnable.

Enumerate from the SAME relay the server consumes from. Recommended (the
`-w /app` + direct exec avoids inner-shell JSON escaping):

    docker exec -w /app neofry ./strfry scan '{"kinds":[1984,5]}' \
      | poetry run python -m scripts.backfill_user_reports --scan - [--apply]

    # in k8s:
    kubectl exec -n <ns> deploy/neofry -- ./strfry scan '{"kinds":[1984,5]}' \
      | poetry run python -m scripts.backfill_user_reports --scan - [--apply]

Imports app.core.config, which validates settings. PUBLIC_BASE_URL (unused here)
is defaulted below so a partial local .env still runs; if your .env omits some
OTHER required field, prefix it, e.g. `SOME_FIELD=... poetry run ...`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator

# Allow running as a plain script as well as `python -m scripts.backfill_user_reports`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# app.core.config validates ALL settings at import. This script only touches
# Redis + Neo4j, so supply a harmless placeholder for an unrelated required
# HTTP field to run against a partial local .env. `setdefault` never overrides
# a real value, so staging/prod (full .env) are unaffected.
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from app.core.redis_db import redis_client  # noqa: E402
from app.message_queue_tasks.process_strfry_event import (  # noqa: E402
    REPORTED_BY_KEY_PREFIX,
)
from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402
from app.services.report_backfill_service import (  # noqa: E402
    ReportedByDiff,
    build_desired_reported_by,
    diff_reported_by,
)

_REDIS_PIPE_CHUNK = 1000


def _read_events(scan: str) -> Iterator[dict]:
    fh = sys.stdin if scan == "-" else open(scan, encoding="utf-8")
    try:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
    finally:
        if fh is not sys.stdin:
            fh.close()


def _split_by_kind(events: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    reports, deletions = [], []
    for ev in events:
        kind = ev.get("kind")
        if kind == 1984:
            reports.append(ev)
        elif kind == 5:
            deletions.append(ev)
    return reports, deletions


async def _load_current_reported_by() -> dict[str, set[str]]:
    current: dict[str, set[str]] = {}
    async for key in redis_client.scan_iter(
        match=f"{REPORTED_BY_KEY_PREFIX}*", count=500
    ):
        members = await redis_client.smembers(key)
        if members:
            current[key[len(REPORTED_BY_KEY_PREFIX) :]] = set(members)
    return current


async def _apply_redis(diff: ReportedByDiff) -> None:
    pipe = redis_client.pipeline(transaction=False)
    pending = 0
    ops = [("sadd", t, m) for t, ms in diff.to_add.items() for m in ms]
    ops += [("srem", t, m) for t, ms in diff.to_remove.items() for m in ms]
    for op, target, member in ops:
        getattr(pipe, op)(f"{REPORTED_BY_KEY_PREFIX}{target}", member)
        pending += 1
        if pending >= _REDIS_PIPE_CHUNK:
            await pipe.execute()
            pipe = redis_client.pipeline(transaction=False)
            pending = 0
    if pending:
        await pipe.execute()


async def _rebuild_neo4j(desired: dict[str, set[str]], chunk: int) -> int:
    # Full rebuild: REPORTS edges carry no properties, so drop-and-remerge is
    # lossless. Serial writes only — Neo4j fails under concurrent writers.
    pairs = [(r, t) for t, reporters in desired.items() for r in reporters]
    async with neo4j_driver.session() as session:
        await session.run("MATCH (:NostrUser)-[r:REPORTS]->(:NostrUser) DELETE r")
        for i in range(0, len(pairs), chunk):
            batch = [{"reporter": r, "target": t} for r, t in pairs[i : i + chunk]]
            await session.run(
                """
                UNWIND $pairs AS p
                MERGE (a:NostrUser {pubkey: p.reporter})
                MERGE (b:NostrUser {pubkey: p.target})
                MERGE (a)-[:REPORTS]->(b)
                """,
                pairs=batch,
            )
    return len(pairs)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scan",
        required=True,
        help="strfry-scan NDJSON of kind 1984 + kind 5, or '-' for stdin",
    )
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    ap.add_argument("--chunk", type=int, default=5000, help="Neo4j merge batch size")
    args = ap.parse_args()

    t0 = time.monotonic()
    reports, deletions = _split_by_kind(_read_events(args.scan))
    desired = build_desired_reported_by(reports, deletions)
    current = await _load_current_reported_by()
    diff = diff_reported_by(desired, current)

    desired_pairs = sum(len(v) for v in desired.values())
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    log(f"scanned: {len(reports)} reports, {len(deletions)} deletions")
    log(f"desired: {desired_pairs} reported_by pairs across {len(desired)} targets")
    log(f"redis diff: +{diff.add_count} / -{diff.remove_count}")

    if not args.apply:
        log(f"[dry-run] no writes. elapsed {time.monotonic() - t0:.2f}s")
        return

    edges = await _rebuild_neo4j(desired, args.chunk)
    await _apply_redis(diff)
    log(
        f"[applied] neo4j REPORTS: {edges} edges; "
        f"redis +{diff.add_count}/-{diff.remove_count}. "
        f"elapsed {time.monotonic() - t0:.2f}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
