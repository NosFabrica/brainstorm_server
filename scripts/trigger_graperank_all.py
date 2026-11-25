"""Bulk-trigger GrapeRank for every observer — paced, resumable, with status.

Re-runs GrapeRank for ALL observer perspectives (rows in `brainstorm_nsec`) so
each observer's published TAs get re-asserted (e.g. after a new tag is added).
GrapeRank is expensive per observer, so enqueues are RATE-LIMITED.

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
    --observers F   only the Observers in this file (one pubkey a line).
    --assigned      with --observers: also every Observer on an enabled plan of
                    its own (the paid ones).
    --source S      what the runs are recorded as: manual (default) or admin
                    (the admin lane on the queue).
    --wait          after enqueuing, wait for every run to end; exit 1 unless
                    all succeeded.

A sample after a change, one a minute so the worker keeps up:

    python -m scripts.trigger_graperank_all --observers sample.txt --assigned \
        --source admin --rate 1 --wait
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
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
    Scheduling,
    TriggerSource,
)
from app.services.brainstorm_request_service import (  # noqa: E402
    create_brainstorm_request,
)
from app.utils.datetimes import utc_now  # noqa: E402

_GRAPERANK = "graperank"
_SUCCESS = BrainstormRequestStatus.SUCCESS.value
_FAILURE = BrainstormRequestStatus.FAILURE.value
_PUBKEY = re.compile(r"[0-9a-f]{64}")
_WAIT_POLL_SECONDS = 15
_DEFAULT_STATE = "graperank_backfill_state.json"


def _resolve_cutoff(args) -> datetime:
    """Campaign-start cutoff: --since wins; else read the state file; else stamp
    'now' and persist it so future runs resume against the same campaign."""
    if args.since:
        return datetime.fromisoformat(args.since)

    state_path = Path(args.state_file)
    if state_path.exists():
        data = json.loads(state_path.read_text())
        return datetime.fromisoformat(data["campaign_started_at"])

    started = utc_now()
    if not args.status and not args.dry_run:
        state_path.write_text(
            json.dumps({"campaign_started_at": started.isoformat()}, indent=2)
        )
        print(f"[campaign] started at {started.isoformat()} (saved to {state_path})")
    return started


def read_observers(path: str) -> list[str]:
    """The pubkeys in a file, one a line, in order; blank lines and `#` comments
    are skipped. Anything else that is not a pubkey is an error."""
    keys: dict[str, None] = {}
    for line in Path(path).read_text().splitlines():
        key = line.split("#", 1)[0].strip()
        if not key:
            continue
        if not _PUBKEY.fullmatch(key):
            raise ValueError(f"{path}: not a 64-char lowercase hex pubkey: {key!r:.80}")
        keys[key] = None
    return list(keys)


def choose(everyone: list[str], listed: list[str] | None, assigned: list[str]):
    """Whom this campaign runs: everyone, or the listed Observers (then those
    on a plan of their own, when asked for). Never a key that is not an Observer."""
    if listed is None:
        return everyone
    known = set(everyone)
    chosen = dict.fromkeys(pk for pk in [*listed, *assigned] if pk in known)
    return list(chosen)


async def _assigned_observers() -> list[str]:
    async with db_session() as db:
        rows = await db.execute(
            select(BrainstormNsec.pubkey)
            .join(Scheduling, Scheduling.id == BrainstormNsec.scheduling_id)
            .where(Scheduling.enabled.is_(True))
            .order_by(BrainstormNsec.pubkey)
        )
        return list(rows.scalars().all())


async def _load_state(cutoff: datetime):
    """Return (all_observers, triggered_set, completed_set) for this campaign."""
    async with db_session() as db:
        observers = (await db.execute(select(BrainstormNsec.pubkey))).scalars().all()
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


async def _trigger_one(pubkey: str, source: str = TriggerSource.MANUAL.value) -> None:
    async with db_session() as db:
        await create_brainstorm_request(
            db=db,
            algorithm=_GRAPERANK,
            parameters=pubkey,
            pubkey=pubkey,
            nsec_exists=True,  # observers in brainstorm_nsec already have one
            trigger_source=source,
        )


async def _wait(chosen: list[str], cutoff: datetime, timeout_minutes: float) -> int:
    """Until every chosen Observer's newest run since `cutoff` has ended. 0 when
    all succeeded."""
    deadline = asyncio.get_running_loop().time() + timeout_minutes * 60
    while True:
        async with db_session() as db:
            rows = (
                await db.execute(
                    select(
                        BrainstormRequest.pubkey,
                        BrainstormRequest.status,
                        BrainstormRequest.created_at,
                        BrainstormRequest.updated_at,
                    )
                    .where(
                        BrainstormRequest.algorithm == _GRAPERANK,
                        BrainstormRequest.created_at >= cutoff,
                        BrainstormRequest.pubkey.in_(chosen),
                    )
                    .order_by(BrainstormRequest.created_at)
                )
            ).all()
        newest = {
            pubkey: (status, created, updated)
            for pubkey, status, created, updated in rows
        }
        ended = {pk: v for pk, v in newest.items() if v[0] in (_SUCCESS, _FAILURE)}
        if len(ended) == len(chosen) or asyncio.get_running_loop().time() >= deadline:
            break
        print(f"  {len(ended)}/{len(chosen)} runs ended", flush=True)
        await asyncio.sleep(_WAIT_POLL_SECONDS)

    failed = sorted(pk for pk, v in ended.items() if v[0] == _FAILURE)
    unfinished = sorted(pk for pk in chosen if pk not in ended)
    seconds = sorted(
        (v[2] - v[1]).total_seconds() for v in ended.values() if v[0] == _SUCCESS
    )
    print(
        f"runs: {len(chosen)}, succeeded: {len(seconds)}, failed: {len(failed)}, "
        f"not ended: {len(unfinished)}"
    )
    if seconds:
        print(
            f"seconds per run: min {seconds[0]:.1f}, median "
            f"{seconds[len(seconds) // 2]:.1f}, max {seconds[-1]:.1f}"
        )
    for pk in failed:
        print(f"  failed: {pk}")
    for pk in unfinished:
        print(f"  not ended: {pk}")
    return 0 if len(seconds) == len(chosen) else 1


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rate", type=float, default=20.0, help="enqueues per minute")
    p.add_argument("--limit", type=int, default=None, help="max to enqueue this run")
    p.add_argument("--since", default=None, help="ISO cutoff; overrides state file")
    p.add_argument("--state-file", default=_DEFAULT_STATE)
    p.add_argument("--status", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--observers", default=None, help="file of pubkeys to run")
    p.add_argument("--assigned", action="store_true")
    p.add_argument(
        "--source",
        choices=[TriggerSource.MANUAL.value, TriggerSource.ADMIN.value],
        default=TriggerSource.MANUAL.value,
    )
    p.add_argument("--wait", action="store_true")
    p.add_argument("--wait-minutes", type=float, default=180.0)
    args = p.parse_args()
    if args.assigned and not args.observers:
        p.error("--assigned goes with --observers")

    cutoff = _resolve_cutoff(args)
    observers, triggered, completed = await _load_state(cutoff)
    listed = read_observers(args.observers) if args.observers else None
    observers = choose(
        observers, listed, await _assigned_observers() if args.assigned else []
    )
    if listed is not None:
        print(f"listed: {len(listed)}, chosen with plans: {len(observers)}")

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
    refused: list[str] = []
    for o in to_trigger:
        try:
            await _trigger_one(o, args.source)
            enqueued += 1
            if enqueued % 25 == 0:
                print(f"  enqueued {enqueued}/{len(to_trigger)}", flush=True)
        except Exception as exc:  # noqa: BLE001 — keep going; resume covers gaps
            refused.append(o)
            print(f"  FAILED to trigger {o}: {exc!r}", file=sys.stderr)
        if delay:
            await asyncio.sleep(delay)

    print(
        f"done: enqueued {enqueued} graperank request(s). "
        f"Re-run to continue, or --status to watch completion."
    )
    if args.wait:
        # A run that was refused (a compromised key) never ends: not waited for.
        waited = await _wait(
            [o for o in observers if o not in refused], cutoff, args.wait_minutes
        )
        return 1 if refused else waited
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
