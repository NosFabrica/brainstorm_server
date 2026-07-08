"""
List all observers and their derived signing pubkeys.

For each row in the `brainstorm_nsec` table this prints:
    <observer_pubkey>\t<signing_pubkey>

The signing pubkey is what appears as the `pubkey` field on the kind-30382
TA-score events published to the relay. Because the relay aggregates events
across all environments, you must run this inside the env (pod) whose DB
you want to enumerate.

Usage (from a brainstorm-server pod):
    python -m scripts.list_observer_signers
    python -m scripts.list_observer_signers --tsv > signers.tsv
    python -m scripts.list_observer_signers --signers-only
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a plain script: `python scripts/list_observer_signers.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import defer  # noqa: E402
from nostr_sdk import Keys  # noqa: E402

from app.core.database import db_session  # noqa: E402
from app.db_models import BrainstormNsec  # noqa: E402
from app.utils.encryption import decrypt_nsec, load_keys_from_file  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signers-only",
        action="store_true",
        help="Only print signing pubkeys (one per line).",
    )
    parser.add_argument(
        "--tsv",
        action="store_true",
        help="Print a header line and tab-separated columns.",
    )
    args = parser.parse_args()
    load_keys_from_file()  # standalone process: bootstrap the nsec key file to decrypt

    async with db_session() as db:
        # Only nsec/pubkey are read below; defer the heavy columns so this
        # doesn't pull every row's multi-MB last_published_pubkeys blob.
        rows = (
            await db.execute(
                select(BrainstormNsec).options(
                    defer(BrainstormNsec.last_published_pubkeys),
                    defer(BrainstormNsec.last_published_graperank_request_id),
                    defer(BrainstormNsec.graperank_preset),
                    defer(BrainstormNsec.graperank_custom_params),
                )
            )
        ).scalars().all()

    if args.tsv and not args.signers_only:
        print("observer_pubkey\tsigning_pubkey")

    for row in rows:
        nsec = decrypt_nsec(row.encrypted_nsec) if row.encrypted_nsec else row.nsec
        try:
            signer = Keys.parse(secret_key=nsec).public_key().to_hex()
        except Exception as e:  # noqa: BLE001
            print(f"# failed to parse nsec for observer {row.pubkey}: {e}",
                  file=sys.stderr)
            continue

        if args.signers_only:
            print(signer)
        else:
            print(f"{row.pubkey}\t{signer}")


if __name__ == "__main__":
    asyncio.run(main())
