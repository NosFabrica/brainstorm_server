"""Delete published state for ONE observer, per sink. Operator-run, in-env.

Two targets:
  * default        — delete ORPHANS only (sink contents NOT in `last_published`:
                     legacy sub-cutoff cells, best-effort-write leftovers, and
                     ghosts — vanished-from-graph entries no resync can reap).
  * --wipe         — delete the observer's ENTIRE enumerated footprint on the
                     sink (incl. current truth). For deprovisioning or a full
                     reset; follow with `resync?target=both` to repopulate (brief
                     window where the observer is empty). Bypasses the safety cap.

DRY-RUN by default (reports counts + a sample); pass --apply to delete. "Missing"
(under-delivered) is NOT handled here — that's a full resync.

Scope is ONE observer (--observer <hex>) — everything stays bounded to that
observer's cells/TAs (the Vespa visit records only their cells, not the corpus).
For bulk drift set *_SWEEP_BELOW_CUTOFF (resync/the backstop do NOT sweep).

Safety cap (default target, --apply only): if the orphan count exceeds
--max-orphans (abs) or --max-orphan-pct (% of published), the delete is SKIPPED
and flagged — a huge count is usually the legacy pre-cutoff-fix backlog, which
the `*_SWEEP_BELOW_CUTOFF` drain reaps (but NOT ghosts) more safely than a raw
delete.

RUN IT INSIDE THE brainstorm-server POD/CONTAINER — DB, VESPA_URL, and the nsec
key file (/run/secrets/nsec_encryption_keys) are already wired; nothing to
port-forward. Commands use `poetry run` (the image runs its deps in a venv).

Vespa (self-contained; no key needed):
    kubectl exec <server-pod> -- \
        poetry run python -m scripts.clean_observer_orphans --observer <hex> --sink vespa
    kubectl exec <server-pod> -- \
        poetry run python -m scripts.clean_observer_orphans --observer <hex> --sink vespa --wipe --apply

Relay (scan in the RELAY pod, sign+delete in the SERVER pod where the key lives):
    # <signer> via: poetry run python -m scripts.list_observer_signers | grep <observer>
    kubectl exec <strfry-pod> -- /app/strfry scan '{"kinds":[30382],"authors":["<signer>"]}' \
      | kubectl exec -i <server-pod> -- \
          poetry run python -m scripts.clean_observer_orphans --observer <hex> --sink relay --scan - --apply

Docker: swap `kubectl exec [-i] <pod>` for `docker exec [-i] <container>`.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nostr_sdk import Keys  # noqa: E402

from app.core.database import db_session  # noqa: E402
from app.core.vespa import batch_upsert_scores, visit_observer_cells  # noqa: E402
from app.message_queue_tasks.upload_nostr_events import (  # noqa: E402
    get_deletion_events_for_dropped_pubkeys,
    init_nostr_client,
)
from app.repos.brainstorm_nsec import (  # noqa: E402
    get_last_published_pubkeys_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.services.observer_sweep_service import orphans_of, parse_ta_scan  # noqa: E402
from app.utils.encryption import decrypt_nsec, load_keys_from_file  # noqa: E402


def _capped(items: set[str], published: list[str], args) -> str | None:
    """Reason this delete set is too large to auto-delete, or None."""
    if len(items) > args.max_orphans:
        return f"{len(items)} > --max-orphans {args.max_orphans}"
    if published and len(items) * 100 > args.max_orphan_pct * len(published):
        return f"{len(items)} > {args.max_orphan_pct}% of {len(published)} published"
    return None


def _report(tag: str, verb: str, items: set[str], cap: str | None) -> None:
    suffix = f"  [CAPPED: {cap} → skip --apply; use resync]" if cap else ""
    print(f"{tag} {verb}={len(items)}{suffix}")
    for pk in sorted(items)[:3]:
        print(f"    {pk}")
    if len(items) > 3:
        print(f"    ... and {len(items) - 3} more")


async def _delete_vespa(observer: str, items: set[str]) -> None:
    ok, failed = await batch_upsert_scores(
        upserts=[], removes=sorted(items), observer=observer
    )
    print(f"[vespa {observer[:12]}] removed ok={ok} failed={failed}")


async def _delete_relay(observer: str, nsec: str, signer: str, items: set[str]) -> None:
    client = await init_nostr_client(nsec)
    try:
        events = await get_deletion_events_for_dropped_pubkeys(
            observees=sorted(items), signing_pubkey=signer, nostr_client=client
        )
        sent = 0
        for ev in events:
            if (await client.send_event(ev)).success:
                sent += 1
        print(f"[relay {observer[:12]}] sent {sent}/{len(events)} kind-5 deletions")
    finally:
        await client.disconnect()


def _delete_set(present: set[str], published: list[str], wipe: bool) -> set[str]:
    """--wipe → everything the sink holds; else → orphans (present − published)."""
    return set(present) if wipe else orphans_of(present, published)


async def run(args, by_signer: dict[str, set[str]]) -> None:
    mode = "APPLY" if args.apply else "DRY-RUN"
    verb = "WIPE-ALL" if args.wipe else "orphans"
    observer = args.observer
    print(f"=== clean {observer[:12]} sink={args.sink} target={verb} [{mode}] ===")
    if args.wipe:
        print("!! WIPE deletes the observer's ENTIRE published set on this sink "
              "(incl. truth) — follow with resync?target=both to repopulate.")

    async with db_session() as db:
        load_keys_from_file()  # standalone process: bootstrap the nsec key file
        row, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, observer
        )
        published = await get_last_published_pubkeys_by_pubkey_on_db(db, observer)
        nsec = None
        if args.sink in ("relay", "both"):
            nsec = decrypt_nsec(row.encrypted_nsec) if row.encrypted_nsec else row.nsec
    print(f"last_published: {len(published)}")

    if args.sink in ("vespa", "both"):
        # Filtered to one observer → the visit records only this observer's cells
        # (bounded ~ its published count; NOT the all-observers OOM path).
        cells = (await visit_observer_cells({observer})).get(observer, set())
        items = _delete_set(cells, published, args.wipe)
        cap = None if args.wipe else (_capped(items, published, args) if args.apply else None)
        _report(f"[vespa {observer[:12]}]", verb, items, cap)
        if args.apply and items and not cap:
            await _delete_vespa(observer, items)
    if args.sink in ("relay", "both"):
        signer = Keys.parse(secret_key=nsec).public_key().to_hex()
        items = _delete_set(by_signer.get(signer, set()), published, args.wipe)
        cap = None if args.wipe else (_capped(items, published, args) if args.apply else None)
        _report(f"[relay {observer[:12]}]", verb, items, cap)
        if args.apply and items and not cap:
            await _delete_relay(observer, nsec, signer, items)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", required=True, help="Observer pubkey (hex).")
    # Per observer only: the Vespa visit records just this observer's cells, so
    # memory stays bounded. For bulk drift set *_SWEEP_BELOW_CUTOFF.
    parser.add_argument("--sink", choices=["vespa", "relay", "both"], default="vespa")
    parser.add_argument(
        "--scan", help="strfry-scan JSONL, or '-' for stdin (required for relay/both)."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually delete (default: dry-run)."
    )
    parser.add_argument(
        "--wipe", action="store_true",
        help="Delete the observer's ENTIRE published set (incl. truth), not just "
             "orphans; bypasses the safety cap. Follow with a resync to repopulate.",
    )
    parser.add_argument(
        "--max-orphans", type=int, default=5000,
        help="orphan-mode --apply cap: skip if more orphans than this.",
    )
    parser.add_argument(
        "--max-orphan-pct", type=float, default=10.0,
        help="orphan-mode --apply cap: skip if orphans exceed this %% of published.",
    )
    args = parser.parse_args()
    if args.sink in ("relay", "both") and not args.scan:
        parser.error("--scan is required for --sink relay/both")
    if args.wipe and args.sink != "vespa":
        parser.error("--wipe is vespa-only; relay orphans are cleaned normally")
    return args


if __name__ == "__main__":
    args = _parse_args()
    # Parse the scan dump (stdin or file) up front, before touching the DB.
    by_signer: dict[str, set[str]] = {}
    if args.scan:
        if args.scan == "-":
            by_signer = parse_ta_scan(sys.stdin)
        else:
            with open(args.scan) as f:
                by_signer = parse_ta_scan(f)
    asyncio.run(run(args, by_signer))
