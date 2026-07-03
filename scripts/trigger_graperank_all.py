"""Bulk-trigger GrapeRank for every observer — paced, resumable, with status.

Re-runs GrapeRank for ALL observer perspectives (rows in `brainstorm_nsec`) so
each observer's Vespa tensors (`quality_scores` + the new `follower_counts`) get
repopulated — e.g. the follower-count backfill in docs/search-vs-tapestry.md
§8/§9. GrapeRank is expensive per observer, so enqueues are RATE-LIMITED.

"Done" is read from the `brainstorm_request` table, so there's no fragile
bookkeeping: for a campaign that started at time T, an observer is
  * triggered  → it has a `graperank` request created at/after T, and
  * completed  → that request reached status `success`.
The campaign start T is stored in a small state file so re-running the script
RESUMES (skips already-triggered observers) without re-enqueuing. The DB is the
source of truth; the state file only remembers when the campaign began.

Run from a brainstorm-server pod / the container (needs a populated `.env`):

    # How many observers, how many done, how many pending (no writes):
    python -m scripts.trigger_graperank_all --status

    # Preview the next batch without enqueuing:
    python -m scripts.trigger_graperank_all --dry-run --limit 50

    # Enqueue up to 200 observers this run at 30/min, then resume later by
    # just running again (already-triggered observers are skipped):
    python -m scripts.trigger_graperank_all --rate 30 --limit 200
    python -m scripts.trigger_graperank_all --rate 30

Flags:
    --rate N        observers to ENQUEUE per minute (default 20). Tune to the
                    graperank worker's throughput so the queue doesn't back up.
    --limit N       max observers to enqueue this invocation (default: no cap).
    --since ISO     override the campaign-start cutoff (else read/written to the
                    state file). Pass a NEW value to start a fresh campaign.
    --state-file P  campaign-state json (default ./graperank_backfill_state.json).
    --status        print progress and exit (no enqueue).
    --dry-run       list what WOULD be enqueued; don't enqueue.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.database import db_session  # noqa: E402
from app.db_models import (  # noqa: E402
    BrainstormNsec,
    BrainstormRequest,
    BrainstormRequestStatus,
)
from app.services.brainstorm_request_service import (  # noqa: E402
    create_brainstorm_request,
)

_GRAPERANK = "graperank"
_SUCCESS = BrainstormRequestStatus.SUCCESS.value
_DEFAULT_STATE = "graperank_backfill_state.json"


def _now() -> datetime:
    # Naive UTC to match the `DateTime` (tz-naive) created_at columns and avoid
    # aware-vs-naive comparison errors in the WHERE clause.
    return datetime.utcnow()


def _resolve_cutoff(args) -> datetime:
    """Campaign-start cutoff: --since wins; else read the state file; else stamp
    'now' and persist it so future runs resume against the same campaign."""
    if args.since:
        return datetime.fromisoformat(args.since)

    state_path = Path(args.state_file)
    if state_path.exists():
        data = json.loads(state_path.read_text())
        return datetime.fromisoformat(data["campaign_started_at"])

    started = _now()
    if not args.status and not args.dry_run:
        state_path.write_text(
            json.dumps({"campaign_started_at": started.isoformat()}, indent=2)
        )
        print(f"[campaign] started at {started.isoformat()} (saved to {state_path})")
    return started


async def _load_state(cutoff: datetime):
    """Return (all_observers, triggered_set, completed_set) for this campaign."""
    async with db_session() as db:
        observers = (
            (await db.execute(select(BrainstormNsec.pubkey))).scalars().all()
        )
        rows = (
            await db.execute(
                select(BrainstormRequest.pubkey, BrainstormRequest.status).where(
                    BrainstormRequest.algorithm == _GRAPERANK,
                    BrainstormRequest.created_at >= cutoff,
                )
            )
        ).all()

    triggered: set[str] = set()
    completed: set[str] = set()
    for pubkey, status in rows:
        if not pubkey:
            continue
        triggered.add(pubkey)
        if status == _SUCCESS:
            completed.add(pubkey)
    return sorted(set(observers)), triggered, completed


def _print_status(observers, triggered, completed, cutoff) -> None:
    total = len(observers)
    obs = set(observers)
    n_trig = len(triggered & obs)
    n_done = len(completed & obs)
    n_inflight = n_trig - n_done
    n_pending = total - n_trig
    print(f"campaign start : {cutoff.isoformat()}")
    print(f"observers      : {total}")
    print(f"  completed    : {n_done}  (graperank success since campaign start)")
    print(f"  in-flight    : {n_inflight}  (triggered, not yet success)")
    print(f"  pending      : {n_pending}  (not yet triggered)")


async def _trigger_one(pubkey: str) -> None:
    async with db_session() as db:
        await create_brainstorm_request(
            db=db,
            algorithm=_GRAPERANK,
            parameters=pubkey,
            pubkey=pubkey,
            nsec_exists=True,  # observers in brainstorm_nsec already have one
        )


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rate", type=float, default=20.0, help="enqueues per minute")
    p.add_argument("--limit", type=int, default=None, help="max to enqueue this run")
    p.add_argument("--since", default=None, help="ISO cutoff; overrides state file")
    p.add_argument("--state-file", default=_DEFAULT_STATE)
    p.add_argument("--status", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cutoff = _resolve_cutoff(args)
    observers, triggered, completed = await _load_state(cutoff)

    if args.status:
        _print_status(observers, triggered, completed, cutoff)
        return 0

    to_trigger = [o for o in observers if o not in triggered]
    if args.limit is not None:
        to_trigger = to_trigger[: args.limit]

    _print_status(observers, triggered, completed, cutoff)
    print(f"to enqueue now : {len(to_trigger)}  (rate={args.rate}/min)")

    if args.dry_run:
        for o in to_trigger[:20]:
            print(f"  would trigger {o}")
        if len(to_trigger) > 20:
            print(f"  ... and {len(to_trigger) - 20} more")
        return 0

    delay = 60.0 / args.rate if args.rate > 0 else 0.0
    enqueued = 0
    for o in to_trigger:
        try:
            await _trigger_one(o)
            enqueued += 1
            if enqueued % 25 == 0:
                print(f"  enqueued {enqueued}/{len(to_trigger)}")
        except Exception as exc:  # noqa: BLE001 — keep going; resume covers gaps
            print(f"  FAILED to trigger {o}: {exc!r}", file=sys.stderr)
        if delay:
            await asyncio.sleep(delay)

    print(f"done: enqueued {enqueued} graperank request(s). "
          f"Re-run to continue, or --status to watch completion.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
