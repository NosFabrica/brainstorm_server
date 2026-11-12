"""Backfill: replay the kind-39999 events the relay already holds through ingest.

Event taggings and tagging headers (ADR `trusted-lists/0002` D6, D10) were
dropped at parse until the parsers for them shipped, and neofry forwards an
event to the `strfry:events` queue only when it first stores it, so nothing
delivers them again. This runs each kind-39999 event through
`process_event_kind_39999`, the live handler, so the result is exactly what
live ingest would have written.

DRY-RUN by default (prints counts, writes nothing). Pass --apply to write.
Idempotent: every upsert is latest-wins on the event's own `created_at`, so a
re-run changes nothing, and a tagging that arrives live meanwhile is never
overwritten by an older one. Run it after the release with the new parsers is
serving, so nothing lands between the replay and live ingest.

Enumerate from the relay the server consumes from (neofry):

    docker exec -w /app neofry ./strfry scan '{"kinds":[39999]}' \
      | poetry run python -m scripts.backfill_event_taggings --scan - [--apply]

    # in k8s, running the script in a server pod (it ships in the image):
    kubectl -n <ns> exec brainstorm-neofry-0 -c neofry -- \
        /app/strfry scan '{"kinds":[39999]}' \
      | kubectl -n <ns> exec -i deploy/brainstorm-server -- \
        poetry run python -m scripts.backfill_event_taggings --scan - [--apply]

A second input may come from the search relay, which holds a few taggings
published elsewhere (D10), e.g. `nak req -k 39999 --search include:spam
wss://search.brainstorm.world`; duplicates are harmless.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# See backfill_user_reports: a partial local .env still imports app.core.config.
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from app.services.tagging_parse import (  # noqa: E402
    TAGGING_KIND,
    parse_event_tagging,
    parse_tag_element,
    parse_tagging_header,
    parse_user_tagging,
)

SHAPES = ("tag_elements", "pubkey_taggings", "headers", "event_taggings", "ignored")


def read_events(fh: TextIO, unreadable: Counter) -> Iterator[dict]:
    """One event per line. A blank line is skipped; a line that isn't a JSON
    object is counted in `unreadable` and skipped."""
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unreadable["unreadable"] += 1
            continue
        if isinstance(event, dict):
            yield event
        else:
            unreadable["unreadable"] += 1


def shape_of(event: dict) -> str:
    """Which of the four kind-39999 shapes this is, or "ignored"."""
    if event.get("kind") != TAGGING_KIND:
        return "ignored"
    if parse_tag_element(event) is not None:
        return "tag_elements"
    if parse_user_tagging(event) is not None:
        return "pubkey_taggings"
    if parse_tagging_header(event) is not None:
        return "headers"
    if parse_event_tagging(event) is not None:
        return "event_taggings"
    return "ignored"


async def replay(
    events: Iterable[dict],
    *,
    apply: bool,
    handle: Callable[[dict], Awaitable[None]] | None = None,
) -> Counter:
    """Count each shape and, with `apply`, run every recognised event through
    `handle` (the live handler by default). One event's failure is counted and
    skipped, not the end of the replay."""
    if handle is None:
        from app.message_queue_tasks.process_strfry_event import (
            process_event_kind_39999,
        )

        handle = process_event_kind_39999
    counts: Counter = Counter({shape: 0 for shape in SHAPES})
    for event in events:
        shape = shape_of(event)
        counts[shape] += 1
        if not apply or shape == "ignored":
            continue
        try:
            await handle(event)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            print(f"[failed] {event.get('id')}: {exc!r}", file=sys.stderr)
    return counts


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scan", required=True, help="strfry-scan NDJSON of kind 39999, or '-'"
    )
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry-run)"
    )
    args = ap.parse_args()

    t0 = time.monotonic()
    unreadable: Counter = Counter()
    fh = sys.stdin if args.scan == "-" else open(args.scan, encoding="utf-8")
    try:
        counts = await replay(read_events(fh, unreadable), apply=args.apply)
    finally:
        if fh is not sys.stdin:
            fh.close()
    counts.update(unreadable)
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    mode = "applied" if args.apply else "dry-run, no writes"
    print(
        f"[{mode}] {summary}. elapsed {time.monotonic() - t0:.2f}s", file=sys.stderr
    )
    if counts.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
