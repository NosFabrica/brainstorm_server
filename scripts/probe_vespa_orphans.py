"""Probe Vespa published-state drift per observer: how many quality_scores cells
does Vespa hold that are NOT in `last_published` (orphans), and how many published
entries is Vespa missing (missing)? READ-ONLY — diffs, never deletes.

Single-phase (unlike the relay probe): Vespa cells are keyed by the OBSERVER
pubkey and the visit API is plain Vespa HTTP the server can reach, so no relay-pod
exec and no nsec needed. Enumeration is a FULL corpus visit (the only complete
read — search only sees above-cutoff cells), so cost scales with corpus size.

Run in a brainstorm-server pod (needs .env + DB + VESPA_URL):

    poetry run python -m scripts.probe_vespa_orphans --tsv
    poetry run python -m scripts.probe_vespa_orphans --limit 50 --out-dir /tmp/vespa_drift

Observers are processed biggest-first (by last_published size). `--out-dir`
writes the actual orphan/missing pubkey lists per observer (feeds the cleanup).
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import defer  # noqa: E402

from app.core.database import db_session  # noqa: E402
from app.core.vespa import visit_observer_cells  # noqa: E402
from app.db_models import BrainstormNsec  # noqa: E402
from app.repos.brainstorm_nsec import (  # noqa: E402
    get_last_published_pubkeys_by_pubkey_on_db,
)
from app.services.observer_sweep_service import orphans_of  # noqa: E402

_PUBKEY_BYTES = 32


async def _roster(db, observer: str | None, min_published: int, limit: int | None):
    nbytes = func.octet_length(BrainstormNsec.last_published_pubkeys)
    stmt = (
        select(BrainstormNsec.pubkey)
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


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", help="Probe a single observer pubkey (hex).")
    parser.add_argument("--limit", type=int, help="Only the top-N observers.")
    parser.add_argument("--min-published", type=int, default=1)
    parser.add_argument("--out-dir", help="Write <observer>.orphans.txt.")
    parser.add_argument("--tsv", action="store_true")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else None

    async with db_session() as db:
        observers = await _roster(db, args.observer, args.min_published, args.limit)
        targets = set(observers)
        # One full corpus visit covers every target observer at once.
        on_vespa = await visit_observer_cells(targets)

        if args.tsv:
            print("observer\tpublished\tvespa\torphans")

        n = tot_o = obs_orph = 0
        for observer in observers:
            published = await get_last_published_pubkeys_by_pubkey_on_db(db, observer)
            cells = on_vespa.get(observer, set())
            orphans = orphans_of(cells, published)
            n += 1
            tot_o += len(orphans)
            obs_orph += 1 if orphans else 0
            if out_dir:
                _write_list(out_dir, observer, "orphans", orphans)
            print(
                f"{observer}\t{len(published)}\t{len(cells)}\t{len(orphans)}",
                flush=True,
            )

    print(
        f"\n# observers={n} with_orphans={obs_orph} total_orphans={tot_o}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    asyncio.run(main())
