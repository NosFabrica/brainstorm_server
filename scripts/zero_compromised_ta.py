"""Zero the scores of the accounts marked compromised, now, with no GrapeRank run.

The accounts are the `compromised_pubkey` table, which admins edit through
`PUT` / `DELETE /admin/compromised/{pubkey}` (with the event proving the
compromise). Adding one through the API already does what this script does,
in the background; the script is the same work run from a pod, with
per-observer output: for each account, every Observer holding a kind-30382
about it re-signs it at `rank 0` (every count 0, no `hops`), and its stored
Neo4j scores and whitelist entries are cleared.

An Observer is selected if the TA relay holds its 30382 `d=<account>` (this
environment's signers only); when the relay can't be read, if its
`last_published_pubkeys` lists the account. The outcome is recorded on the
account's row (`last_zero_report`).
Every later GrapeRank run keeps the account at 0 — see
`app/services/compromised_service.py`.

Run inside a brainstorm-server pod of each environment (staging, production);
needs `.env` + DB + the nsec encryption key file. Dry-run by default:

    poetry run python -m scripts.zero_compromised_ta
    poetry run python -m scripts.zero_compromised_ta --apply
    poetry run python -m scripts.zero_compromised_ta --apply --pubkey <npub|hex>
    poetry run python -m scripts.zero_compromised_ta --apply --observer <hex>

Re-running is safe. Run it again after a run that was publishing when the
account was added, if that run's TA should not wait for the Observer's next run.

Exit codes: 0 = done (or dry run), 1 = a relay read or publish did not complete,
2 = the account isn't marked compromised / bad argument.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a plain script: `python scripts/zero_compromised_ta.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.compromised_service import (  # noqa: E402
    list_compromised,
    parse_pubkey,
    zero_compromised,
)
from app.utils.encryption import load_keys_from_file  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="Sign, publish, clear.")
    parser.add_argument("--pubkey", help="Only this compromised account (npub or hex).")
    parser.add_argument(
        "--observer", help="Only this Observer's TA (hex); Neo4j/whitelists untouched."
    )
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="Skip the relay read; misses TAs the DB lost track of.",
    )
    args = parser.parse_args()

    load_keys_from_file()  # standalone process: bootstrap the nsec key file
    marked = {row.pubkey: row for row in await list_compromised()}
    if args.pubkey:
        try:
            wanted = parse_pubkey(args.pubkey)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        if wanted not in marked:
            print(
                f"{wanted} is not marked compromised (PUT /admin/compromised)",
                file=sys.stderr,
            )
            return 2
        marked = {wanted: marked[wanted]}
    if not marked:
        print("# no accounts are marked compromised", file=sys.stderr)
        return 0

    ok = True
    for pubkey, row in marked.items():
        report = await zero_compromised(
            pubkey, observer=args.observer, apply=args.apply, db_only=args.db_only
        )
        print(
            f"# {pubkey} (proof {row.proof_event_id}): {len(report.observers)} "
            f"observers hold a TA; relay read {'ok' if report.relay_read else 'FAILED'}",
            file=sys.stderr,
        )
        for observer, signer, source in report.observers:
            print(f"{pubkey}\t{observer}\t{signer}\t{source}")
        if not report.relay_read and not args.db_only:
            ok = False
        if args.apply:
            print(
                f"# published {report.published}, acked {report.acked}, "
                f"neo4j props cleared {report.neo4j_cleared}, whitelists "
                f"{report.whitelists_updated}",
                file=sys.stderr,
            )
            ok = ok and report.complete
    if not args.apply:
        print("# dry run; --apply to publish", file=sys.stderr)
    if not ok:
        print(
            "# INCOMPLETE: re-run (or --db-only if the relay can't be read)",
            file=sys.stderr,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
