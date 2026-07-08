"""Probe relay published-state drift per observer: how many kind-30382 TAs does
the relay hold that are NOT in `last_published` (orphans), and how many published
entries is the relay missing (missing)? READ-ONLY — diffs, never deletes.

Enumeration is via `strfry scan` (REQ can't — strfry caps a filter at
maxFilterLimit=500 and TAs are burst-published, so REQ recovers ~5%). Two phases:

  1) In the RELAY pod, dump every TA:
        strfry scan '{"kinds":[30382]}' > ta_scan.jsonl
     (or a single signer: strfry scan '{"kinds":[30382],"authors":["<sig>"]}')

  2) In a brainstorm-server pod (needs .env + DB + the nsec encryption key file),
     diff it against last_published:
        python -m scripts.probe_relay_orphans --scan ta_scan.jsonl --tsv

Local docker equivalent:
    docker exec strfry sh -c 'cd /app && ./strfry scan "{\"kinds\":[30382]}"' \
      | docker exec -i -w /app brainstorm-server poetry run \
          python -m scripts.probe_relay_orphans --scan - --tsv

Observers are processed biggest-first (by last_published size). `--out-dir`
writes the actual orphan/missing pubkey lists per observer (feeds the reap).
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a plain script: `python scripts/probe_relay_orphans.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nostr_sdk import Keys  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import defer  # noqa: E402

from app.core.database import db_session  # noqa: E402
from app.db_models import BrainstormNsec  # noqa: E402
from app.repos.brainstorm_nsec import (  # noqa: E402
    get_last_published_pubkeys_by_pubkey_on_db,
)
from app.services.observer_sweep_service import (  # noqa: E402
    diff_published,
    parse_ta_scan,
)
from app.utils.encryption import decrypt_nsec, load_keys_from_file  # noqa: E402

# 32-byte packed pubkeys (see app/repos/brainstorm_nsec._pack_pubkeys).
_PUBKEY_BYTES = 32


def _signer_of(row: BrainstormNsec) -> str | None:
    nsec = decrypt_nsec(row.encrypted_nsec) if row.encrypted_nsec else row.nsec
    try:
        return Keys.parse(secret_key=nsec).public_key().to_hex()
    except Exception as e:  # noqa: BLE001
        print(f"# bad nsec for observer {row.pubkey}: {e}", file=sys.stderr)
        return None


async def _roster(db, observer: str | None, min_published: int, limit: int | None):
    """BrainstormNsec rows, biggest published-set first. octet_length keeps the
    ordering/filter off the multi-MB blob (read per-observer later)."""
    nbytes = func.octet_length(BrainstormNsec.last_published_pubkeys)
    stmt = (
        select(BrainstormNsec)
        .options(
            defer(BrainstormNsec.last_published_pubkeys),
            defer(BrainstormNsec.graperank_preset),
            defer(BrainstormNsec.graperank_custom_params),
        )
        .order_by(nbytes.desc().nulls_last())
    )
    if observer:
        stmt = stmt.where(BrainstormNsec.pubkey == observer)
    else:
        stmt = stmt.where(nbytes >= min_published * _PUBKEY_BYTES)
    if limit:
        stmt = stmt.limit(limit)
    return (await db.execute(stmt)).scalars().all()


def _write_list(out_dir: Path, observer: str, suffix: str, pubkeys: set[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{observer}.{suffix}.txt").write_text(
        "\n".join(sorted(pubkeys)) + ("\n" if pubkeys else "")
    )


async def main(by_signer: dict[str, set[str]]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan", required=True,
        help="Path to strfry-scan JSONL, or '-' for stdin (already consumed).",
    )
    parser.add_argument("--observer", help="Probe a single observer pubkey (hex).")
    parser.add_argument("--limit", type=int, help="Only the top-N observers.")
    parser.add_argument(
        "--min-published", type=int, default=1,
        help="Skip observers with fewer than N published pubkeys (default 1).",
    )
    parser.add_argument(
        "--out-dir",
        help="Write <observer>.orphans.txt / .missing.txt (the actual pubkeys).",
    )
    parser.add_argument("--tsv", action="store_true", help="Print a header row.")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else None

    async with db_session() as db:
        load_keys_from_file()  # standalone process: bootstrap the nsec key file
        rows = await _roster(db, args.observer, args.min_published, args.limit)

        if args.tsv:
            print("observer\tsigner\tpublished\trelay\torphans\tmissing")

        n_obs = tot_orphans = tot_missing = obs_with_orphans = 0
        for row in rows:
            signer = _signer_of(row)
            if signer is None:
                continue
            published = await get_last_published_pubkeys_by_pubkey_on_db(db, row.pubkey)
            on_relay = by_signer.get(signer, set())
            orphans, missing = diff_published(on_relay, published)

            n_obs += 1
            tot_orphans += len(orphans)
            tot_missing += len(missing)
            obs_with_orphans += 1 if orphans else 0
            if out_dir:
                _write_list(out_dir, row.pubkey, "orphans", orphans)
                _write_list(out_dir, row.pubkey, "missing", missing)
            print(
                f"{row.pubkey}\t{signer}\t{len(published)}\t{len(on_relay)}\t"
                f"{len(orphans)}\t{len(missing)}",
                flush=True,
            )

    print(
        f"\n# observers={n_obs} with_orphans={obs_with_orphans} "
        f"total_orphans={tot_orphans} total_missing={tot_missing}",
        file=sys.stderr,
    )


def _read_scan() -> dict[str, set[str]]:
    """Parse the scan dump before touching the DB. `--scan -` reads stdin."""
    # argparse runs again inside main(); do a tiny pre-parse just for --scan so we
    # can stream stdin/file into parse_ta_scan without buffering the whole thing.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--scan", required=True)
    scan = pre.parse_known_args()[0].scan
    if scan == "-":
        return parse_ta_scan(sys.stdin)
    with open(scan) as f:
        return parse_ta_scan(f)


if __name__ == "__main__":
    _by_signer = _read_scan()
    asyncio.run(main(_by_signer))
