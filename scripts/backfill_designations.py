"""Backfill: build `DESIGNATES` edges + `designates:` sets from the relay's kind-10040s.

Live ingest (`process_event_kind_10040`) only sees 10040s that arrive after it
shipped; this replays the ones the relay already holds through the same handler,
so the result is exactly what live ingest would have written.

DRY-RUN by default (prints counts, writes nothing). Pass --apply to write.
Idempotent: each author's designations are replaced whole, so re-running is
safe, and a newer 10040 ingested meanwhile is only overwritten by the same
latest event the relay now serves.

Enumerate from the relay the server consumes from (neofry, which the
`designations` router stream fills):

    docker exec -w /app neofry ./strfry scan '{"kinds":[10040]}' \
      | poetry run python -m scripts.backfill_designations --scan - [--apply]

    # in k8s, running the script in a server pod (it ships in the image):
    kubectl -n <ns> exec brainstorm-neofry-0 -c neofry -- \
        /app/strfry scan '{"kinds":[10040]}' \
      | kubectl -n <ns> exec -i deploy/brainstorm-server -- \
        poetry run python -m scripts.backfill_designations --scan - [--apply]
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# See backfill_user_reports: a partial local .env still imports app.core.config.
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from app.message_queue_tasks.process_strfry_event import (  # noqa: E402
    process_event_kind_10040,
)
from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402
from app.services.designation_service import (  # noqa: E402
    DESIGNATION_KIND,
    parse_designated_pubkeys,
)


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


def latest_per_author(events: Iterable[dict]) -> list[dict]:
    """Each author's newest kind-10040. A relay keeps one per author, but a scan
    is not guaranteed to, and replaying an older one would undo a newer one."""
    latest: dict[str, dict] = {}
    for ev in events:
        author = ev.get("pubkey")
        if ev.get("kind") != DESIGNATION_KIND or not isinstance(author, str):
            continue
        held = latest.get(author)
        if held is None or ev.get("created_at", 0) > held.get("created_at", 0):
            latest[author] = ev
    return list(latest.values())


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scan", required=True, help="strfry-scan NDJSON of kind 10040, or '-'"
    )
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    args = ap.parse_args()

    t0 = time.monotonic()
    events = latest_per_author(_read_events(args.scan))
    edges = sum(
        len(parse_designated_pubkeys(ev.get("tags") or [], ev["pubkey"]))
        for ev in events
    )
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    log(f"scanned: {len(events)} authors, {edges} designations")

    if not args.apply:
        log(f"[dry-run] no writes. elapsed {time.monotonic() - t0:.2f}s")
        return

    # Serial, one author per write — Neo4j fails under concurrent writers.
    async with neo4j_driver.session() as session:
        for ev in events:
            await process_event_kind_10040(session, ev)
    log(f"[applied] {len(events)} authors. elapsed {time.monotonic() - t0:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
