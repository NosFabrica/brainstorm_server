"""Re-feed kind-0 profiles from the internal strfry into Vespa — paced + resumable.

Backfill lever for the P1 profile fields (docs/search-vs-tapestry.md §8.4/§9):
the transferer only re-syncs kinds 3/10000/1984, so there is no built-in way to
re-process kind-0 → Vespa. This script reads every kind-0 from the internal
strfry and runs each through the SAME ingest path the live consumer uses
(`process_event_kind_0` → content/tags merge → `upsert_profile`). That:

  * folds the deprecated `username` into `name` where `name` is empty (NIP-24),
    and
  * rewrites every profile through the updated indexing pipeline, so the newly
    indexed `nip05`/`lud16`/`website` fields get built for existing docs.

REQUIRES the schema changes (the indexed fields, `username` field removed) to be
DEPLOYED first — otherwise every `upsert_profile` fails on an unknown field.

It walks kind-0 newest→oldest by `until` cursor, de-duplicates by pubkey within
the run (first/newest wins; kind-0 is replaceable so strfry should already keep
only the latest), and writes the cursor to a state file after each page so a
re-run RESUMES from where it stopped.

Run from a brainstorm-server pod / the container (needs a populated `.env` and
network access to strfry + Vespa):

    python -m scripts.refeed_kind0_to_vespa --status        # cursor + processed so far
    python -m scripts.refeed_kind0_to_vespa --dry-run       # fetch + count, no writes
    python -m scripts.refeed_kind0_to_vespa --concurrency 16 --limit 5000
    python -m scripts.refeed_kind0_to_vespa --concurrency 16   # resume until drained

Flags:
    --relay-url URL   strfry websocket (default: settings.nip50_backing_relay_url)
    --concurrency N   parallel upserts to Vespa (default 16)
    --page N          events per strfry REQ page (default 500)
    --limit N         max events to PROCESS this invocation (default: no cap)
    --state-file P    resume cursor json (default ./refeed_kind0_state.json)
    --status          print cursor + processed count and exit
    --dry-run         fetch + count, do not call upsert
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.vespa import aclose as vespa_aclose  # noqa: E402
from app.message_queue_tasks.process_strfry_event import (  # noqa: E402
    process_event_kind_0,
)

_DEFAULT_STATE = "refeed_kind0_state.json"
_SUB = "refeed"


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"cursor": None, "processed": 0}


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


async def _fetch_page(url: str, until: int | None, limit: int, timeout: float) -> list[dict]:
    """One NIP-01 REQ for kind-0, returning the events (newest-first)."""
    flt: dict = {"kinds": [0], "limit": limit}
    if until is not None:
        flt["until"] = until
    req = json.dumps(["REQ", _SUB, flt])
    events: list[dict] = []
    async with websockets.connect(url, open_timeout=timeout, max_size=None) as ws:
        await ws.send(req)
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break  # treat a stall as end-of-page
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, list) or len(msg) < 2 or msg[1] != _SUB:
                continue
            if msg[0] == "EVENT" and len(msg) >= 3:
                events.append(msg[2])
            elif msg[0] in ("EOSE", "CLOSED"):
                try:
                    await ws.send(json.dumps(["CLOSE", _SUB]))
                except Exception:
                    pass
                break
    return events


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--relay-url", default=settings.nip50_backing_relay_url)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--page", type=int, default=500)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--state-file", default=_DEFAULT_STATE)
    p.add_argument("--status", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args()

    state_path = Path(args.state_file)
    state = _load_state(state_path)

    if args.status:
        print(f"relay      : {args.relay_url}")
        print(f"cursor     : {state['cursor']}  (oldest created_at processed; "
              f"None = not started)")
        print(f"processed  : {state['processed']}")
        return 0

    print(f"re-feed kind-0 from {args.relay_url} "
          f"(resume cursor={state['cursor']}, processed={state['processed']})")

    sem = asyncio.Semaphore(args.concurrency)
    seen: set[str] = set()
    processed_this_run = 0
    failed_this_run = 0
    cursor = state["cursor"]

    async def _process(ev: dict) -> None:
        async with sem:
            await process_event_kind_0(ev)

    try:
        while True:
            page = await _fetch_page(args.relay_url, cursor, args.page, args.timeout)
            if not page:
                print("drained: no more kind-0 events.")
                break

            fresh = []
            for ev in page:
                pk = ev.get("pubkey")
                if isinstance(pk, str) and pk not in seen:
                    seen.add(pk)
                    fresh.append(ev)

            if not args.dry_run and fresh:
                # return_exceptions=True so one malformed profile (e.g. Vespa
                # rejecting a leftover control char in `name`) is logged and
                # skipped instead of aborting the whole backfill. The page still
                # completes, so the cursor advances past the bad doc on resume.
                results = await asyncio.gather(
                    *(_process(ev) for ev in fresh), return_exceptions=True
                )
                errs = [r for r in results if isinstance(r, BaseException)]
                if errs:
                    failed_this_run += len(errs)
                    for e in errs[:3]:
                        print(f"  skip (upsert failed): {e!r}")
                    if len(errs) > 3:
                        print(f"  ... +{len(errs) - 3} more upsert failures this page")

            processed_this_run += len(fresh)
            state["processed"] += len(fresh)

            oldest = min(ev.get("created_at", 0) for ev in page)
            # `until` is inclusive; advancing to `oldest` re-fetches the boundary
            # event(s) (the seen-set drops them). Guard against a no-progress loop
            # when a whole full page shares one timestamp.
            if cursor is not None and oldest >= cursor and len(page) >= args.page:
                oldest = cursor - 1
            cursor = oldest
            state["cursor"] = cursor
            if not args.dry_run:
                _save_state(state_path, state)

            print(f"  page: {len(page)} fetched, {len(fresh)} new "
                  f"(run total {processed_this_run}); cursor={cursor}")

            if args.limit is not None and processed_this_run >= args.limit:
                print(f"hit --limit {args.limit}; stopping (re-run to continue).")
                break
            if len(page) < args.page:
                print("drained: last (short) page.")
                break
    finally:
        await vespa_aclose()

    skipped = f", {failed_this_run} skipped (upsert errors)" if failed_this_run else ""
    print(f"done: processed {processed_this_run} profile(s) this run "
          f"({state['processed']} total){skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
