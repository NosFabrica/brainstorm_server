"""Delete orphaned published state — entries a sink holds that are NOT in an
observer's `last_published` (relay TAs / Vespa cells left behind by legacy
sub-cutoff ingest or best-effort write failures). Operator-run, in-env.

DRY-RUN by default (reports counts + a sample); pass --apply to delete. Only
orphans are removed — "missing" (under-delivered) is repaired by a full resync.

Scope: one observer (--observer <hex>) OR the whole population (--all). --all
pays the enumeration ONCE (one Vespa visit / one relay scan covers everyone) and
loops the diff+delete per observer, biggest-first.

Safety cap (--apply only): an observer whose orphan count exceeds --max-orphans
(abs) or --max-orphan-pct (% of published) is SKIPPED and flagged — a huge count
is usually the legacy pre-cutoff-fix backlog, which `resync?target=vespa` reaps
~99.9% and more safely than a blind delete.

RUN IT INSIDE THE brainstorm-server POD/CONTAINER — DB, VESPA_URL, and the nsec
key file (/run/secrets/nsec_encryption_keys) are already wired; nothing to
port-forward. Commands use `poetry run` (the image runs its deps in a venv).

Vespa (self-contained; no key needed):
    kubectl exec <server-pod> -- \
        poetry run python -m scripts.clean_observer_orphans --observer <hex> --sink vespa
    kubectl exec <server-pod> -- \
        poetry run python -m scripts.clean_observer_orphans --all --sink vespa --apply

Relay (scan in the RELAY pod, sign+delete in the SERVER pod where the key lives):
    kubectl exec <strfry-pod> -- strfry scan '{"kinds":[30382]}' \
      | kubectl exec -i <server-pod> -- \
          poetry run python -m scripts.clean_observer_orphans --all --sink relay --scan - --apply

Docker: swap `kubectl exec [-i] <pod>` for `docker exec [-i] <container>`.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nostr_sdk import Keys  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import defer  # noqa: E402

from app.core.database import db_session  # noqa: E402
from app.core.vespa import batch_upsert_scores, visit_observer_cells  # noqa: E402
from app.db_models import BrainstormNsec  # noqa: E402
from app.message_queue_tasks.upload_nostr_events import (  # noqa: E402
    get_deletion_events_for_dropped_pubkeys,
    init_nostr_client,
)
from app.repos.brainstorm_nsec import (  # noqa: E402
    get_last_published_pubkeys_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.services.observer_sweep_service import diff_published, parse_ta_scan  # noqa: E402
from app.utils.encryption import decrypt_nsec, load_keys_from_file  # noqa: E402

_PUBKEY_BYTES = 32


def _capped(orphans: set[str], published: list[str], args) -> str | None:
    """Reason this orphan set is too large to auto-delete, or None."""
    if len(orphans) > args.max_orphans:
        return f"{len(orphans)} > --max-orphans {args.max_orphans}"
    if published and len(orphans) * 100 > args.max_orphan_pct * len(published):
        return f"{len(orphans)} > {args.max_orphan_pct}% of {len(published)} published"
    return None


def _report(tag: str, orphans: set[str], cap: str | None) -> None:
    suffix = f"  [CAPPED: {cap} → skip --apply; use resync]" if cap else ""
    print(f"{tag} orphans={len(orphans)}{suffix}")
    for pk in sorted(orphans)[:3]:
        print(f"    {pk}")
    if len(orphans) > 3:
        print(f"    ... and {len(orphans) - 3} more")


async def _delete_vespa(observer: str, orphans: set[str]) -> None:
    ok, failed = await batch_upsert_scores(
        upserts=[], removes=sorted(orphans), observer=observer
    )
    print(f"[vespa {observer[:12]}] removed ok={ok} failed={failed}")


async def _delete_relay(observer: str, nsec: str, signer: str, orphans: set[str]) -> None:
    client = await init_nostr_client(nsec)
    try:
        events = await get_deletion_events_for_dropped_pubkeys(
            observees=sorted(orphans), signing_pubkey=signer, nostr_client=client
        )
        sent = 0
        for ev in events:
            if (await client.send_event(ev)).success:
                sent += 1
        print(f"[relay {observer[:12]}] sent {sent}/{len(events)} kind-5 deletions")
    finally:
        await client.disconnect()


async def _targets(db, args) -> list[tuple[str, str | None, list[str]]]:
    """(observer, nsec_or_None, last_published) rows to process, biggest-first.
    nsec is resolved only when the relay sink is in play."""
    if args.observer:
        row, _ = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, args.observer
        )
        rows = [row]
    else:
        nbytes = func.octet_length(BrainstormNsec.last_published_pubkeys)
        stmt = (
            select(BrainstormNsec)
            .options(
                defer(BrainstormNsec.last_published_pubkeys),
                defer(BrainstormNsec.graperank_preset),
                defer(BrainstormNsec.graperank_custom_params),
            )
            .where(nbytes >= args.min_published * _PUBKEY_BYTES)
            .order_by(nbytes.desc().nulls_last())
        )
        if args.limit:
            stmt = stmt.limit(args.limit)
        rows = (await db.execute(stmt)).scalars().all()

    need_nsec = args.sink in ("relay", "both")
    out: list[tuple[str, str | None, list[str]]] = []
    for row in rows:
        published = await get_last_published_pubkeys_by_pubkey_on_db(db, row.pubkey)
        nsec = None
        if need_nsec:
            nsec = decrypt_nsec(row.encrypted_nsec) if row.encrypted_nsec else row.nsec
        out.append((row.pubkey, nsec, published))
    return out


async def run(args, by_signer: dict[str, set[str]]) -> None:
    mode = "APPLY" if args.apply else "DRY-RUN"
    scope = "ALL" if args.all else args.observer[:12]
    print(f"=== clean orphans scope={scope} sink={args.sink} [{mode}] ===")

    async with db_session() as db:
        load_keys_from_file()  # standalone process: bootstrap the nsec key file
        targets = await _targets(db, args)
    print(f"observers: {len(targets)}")

    # Enumerate Vespa ONCE for the whole target set (a full corpus visit).
    on_vespa: dict[str, set[str]] = {}
    if args.sink in ("vespa", "both"):
        on_vespa = await visit_observer_cells({o for o, _, _ in targets})

    for observer, nsec, published in targets:
        if args.sink in ("vespa", "both"):
            orphans, _ = diff_published(on_vespa.get(observer, set()), published)
            cap = _capped(orphans, published, args) if args.apply else None
            _report(f"[vespa {observer[:12]}]", orphans, cap)
            if args.apply and orphans and not cap:
                await _delete_vespa(observer, orphans)
        if args.sink in ("relay", "both"):
            signer = Keys.parse(secret_key=nsec).public_key().to_hex()
            orphans, _ = diff_published(by_signer.get(signer, set()), published)
            cap = _capped(orphans, published, args) if args.apply else None
            _report(f"[relay {observer[:12]}]", orphans, cap)
            if args.apply and orphans and not cap:
                await _delete_relay(observer, nsec, signer, orphans)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--observer", help="A single observer pubkey (hex).")
    scope.add_argument("--all", action="store_true", help="Every observer.")
    parser.add_argument("--sink", choices=["vespa", "relay", "both"], default="vespa")
    parser.add_argument(
        "--scan", help="strfry-scan JSONL, or '-' for stdin (required for relay/both)."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually delete (default: dry-run)."
    )
    parser.add_argument("--limit", type=int, help="--all: only the top-N observers.")
    parser.add_argument(
        "--min-published", type=int, default=1,
        help="--all: skip observers with fewer than N published (default 1).",
    )
    parser.add_argument(
        "--max-orphans", type=int, default=5000,
        help="--apply safety cap: skip an observer with more orphans than this.",
    )
    parser.add_argument(
        "--max-orphan-pct", type=float, default=10.0,
        help="--apply safety cap: skip if orphans exceed this %% of published.",
    )
    args = parser.parse_args()
    if args.sink in ("relay", "both") and not args.scan:
        parser.error("--scan is required for --sink relay/both")
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
