"""Copy every Observer's stored scores from Neo4j properties into the Scorecard database.

Reads Neo4j and never writes it. Dry-run by default. See scripts/CLAUDE.md.

    poetry run python -m scripts.copy_scores_to_postgres                     # what it would do
    poetry run python -m scripts.copy_scores_to_postgres --apply [--page N]  # copy; resumes
    poetry run python -m scripts.copy_scores_to_postgres --status
    poetry run python -m scripts.copy_scores_to_postgres --verify            # exit 1 on a mismatch
    poetry run python -m scripts.copy_scores_to_postgres --only <observer> --apply
    poetry run python -m scripts.copy_scores_to_postgres --restart --apply   # forget progress
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from sqlalchemy import func, select, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import db_session  # noqa: E402
from app.core.scorecard_database import (  # noqa: E402
    dispose_scorecard_engine,
    scorecard_session,
)
from app.db_models import BrainstormRequest  # noqa: E402
from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402
from app.repos.brainstorm_request_repo import in_pipeline_condition  # noqa: E402
from app.repos.user_id_repo import ids_for  # noqa: E402

# Neo4j property prefix -> position in a score row. `trusted_reporters_` and
# anything else on the node is ignored.
FIELDS = {"influence": 0, "hops": 1, "trusted_followers": 2}
_OBSERVER = re.compile(r"[0-9a-f]{64}")
_SUFFIX = 65  # "_" + a 64-char pubkey

# One read of each node's whole property map: every Observer's scores on it come
# out of a single walk of the node's property list. Keyset pages off the pubkey index.
_PAGE = """
MATCH (u:NostrUser) WHERE u.pubkey > $after
WITH u ORDER BY u.pubkey LIMIT $limit
RETURN u.pubkey AS pubkey, properties(u) AS props
"""

_STATE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS score_copy_progress (
        only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
        after text NOT NULL DEFAULT '',
        nodes bigint NOT NULL DEFAULT 0,
        rows_without_user_id bigint NOT NULL DEFAULT 0,
        non_finite bigint NOT NULL DEFAULT 0,
        done boolean NOT NULL DEFAULT false
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS score_copy_checksum (
        observer_id integer PRIMARY KEY,
        rows bigint NOT NULL,
        influence double precision NOT NULL,
        hops double precision NOT NULL,
        followers double precision NOT NULL
    )
    """,
)

_TEMP = (
    "CREATE TEMP TABLE copy_in (observer_id integer, observee_id integer,"
    " influence double precision, hops smallint, verified_followers integer)"
    " ON COMMIT DROP"
)
_UPSERT = """
INSERT INTO scorecard AS s (observer_id, observee_id, influence, hops, verified_followers)
SELECT observer_id, observee_id, influence, hops, verified_followers FROM copy_in
ORDER BY observer_id, observee_id
ON CONFLICT (observer_id, observee_id) DO UPDATE
SET influence = EXCLUDED.influence, hops = EXCLUDED.hops,
    verified_followers = EXCLUDED.verified_followers
"""
# What was read, summed per Observer in the same transaction as the rows: a
# resumed copy never counts a page twice.
_ADD_CHECKSUM = """
INSERT INTO score_copy_checksum AS c (observer_id, rows, influence, hops, followers)
SELECT observer_id, count(*), coalesce(sum(influence), 0), coalesce(sum(hops), 0),
       coalesce(sum(verified_followers), 0)
FROM copy_in GROUP BY observer_id
ON CONFLICT (observer_id) DO UPDATE
SET rows = c.rows + EXCLUDED.rows, influence = c.influence + EXCLUDED.influence,
    hops = c.hops + EXCLUDED.hops, followers = c.followers + EXCLUDED.followers
"""
# Readers version an Observer's answers on this; a copied Observer needs one.
_STAMP = """
INSERT INTO scorecard_observer (observer_id, written_at)
SELECT DISTINCT observer_id, now() FROM copy_in
ON CONFLICT (observer_id) DO UPDATE SET written_at = EXCLUDED.written_at
"""
_COMPARE = """
SELECT u.pubkey, c.rows, s.rows, c.influence, s.influence, c.hops, s.hops,
       c.followers, s.followers
FROM score_copy_checksum c
JOIN nostr_user u ON u.id = c.observer_id
LEFT JOIN (
    SELECT observer_id, count(*) AS rows, coalesce(sum(influence), 0) AS influence,
           coalesce(sum(hops), 0)::double precision AS hops,
           coalesce(sum(verified_followers), 0)::double precision AS followers
    FROM scorecard GROUP BY observer_id
) s USING (observer_id)
"""


@dataclass
class Progress:
    after: str = ""
    nodes: int = 0
    rows_without_user_id: int = 0
    non_finite: int = 0
    done: bool = False


@dataclass
class Page:
    """One page of nodes turned into score rows."""

    rows: list[tuple] = field(default_factory=list)
    rows_without_user_id: int = 0
    non_finite: int = 0


def _number(value, cast):
    """A stored value as `cast`; None for a missing, NaN or infinite one."""
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, float) and not math.isfinite(value):
        return None, True
    return cast(value), False


def scores_on_node(props: dict) -> dict[str, list]:
    """Each Observer's [influence, hops, trusted_followers] on one node."""
    found: dict[str, list] = {}
    for key, value in props.items():
        if len(key) <= _SUFFIX or key[-_SUFFIX] != "_":
            continue
        position = FIELDS.get(key[:-_SUFFIX])
        if position is None:
            continue
        observer = key[-64:]
        if not _OBSERVER.fullmatch(observer):
            continue
        found.setdefault(observer, [None, None, None])[position] = value
    return found


async def _observer_ids(known: dict[str, int], observers: set[str]) -> None:
    missing = [o for o in observers if o not in known]
    if missing:
        # An Observer whose scores exist gets an id even if its node has none yet.
        known.update(await ids_for(missing))


async def build_page(
    nodes: list[dict], observer_ids: dict[str, int], only: str | None = None
) -> Page:
    page = Page()
    per_node = []
    for node in nodes:
        scores = scores_on_node(node["props"])
        if only is not None:
            scores = {only: scores[only]} if only in scores else {}
        if scores:
            per_node.append((node["props"].get("user_id"), scores))
    await _observer_ids(observer_ids, {o for _, scores in per_node for o in scores})
    for user_id, scores in per_node:
        if user_id is None:
            page.rows_without_user_id += len(scores)
            continue
        for observer, (influence, hops, followers) in scores.items():
            influence, bad = _number(influence, float)
            page.non_finite += bad
            page.rows.append(
                (
                    observer_ids[observer],
                    user_id,
                    influence,
                    _number(hops, int)[0],
                    _number(followers, int)[0],
                )
            )
    return page


async def _read_page(session, after: str, limit: int) -> list[dict]:
    async def work(tx):
        result = await tx.run(_PAGE, after=after, limit=limit)
        return [dict(record) async for record in result]

    # A read transaction: Neo4j refuses a write inside it.
    return await session.execute_read(work)


async def _copy_rows(db, rows: list[tuple]) -> None:
    await db.execute(text(_TEMP))
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    await raw.driver_connection.copy_records_to_table(  # type: ignore[union-attr]
        "copy_in",
        records=rows,
        columns=[
            "observer_id",
            "observee_id",
            "influence",
            "hops",
            "verified_followers",
        ],
    )
    await db.execute(text(_UPSERT))
    await db.execute(text(_ADD_CHECKSUM))
    await db.execute(text(_STAMP))


async def ensure_state() -> None:
    async with scorecard_session() as db:
        for statement in _STATE_TABLES:
            await db.execute(text(statement))
        await db.execute(
            text(
                "INSERT INTO score_copy_progress DEFAULT VALUES ON CONFLICT DO NOTHING"
            )
        )


async def read_progress() -> Progress:
    async with scorecard_session() as db:
        row = (
            await db.execute(
                text(
                    "SELECT after, nodes, rows_without_user_id, non_finite, done"
                    " FROM score_copy_progress"
                )
            )
        ).one()
    return Progress(*row)


async def copy_all(session, page_size: int, log=print) -> Progress:
    """Sweep every user node once, from where the last run stopped."""
    await ensure_state()
    progress = await read_progress()
    if progress.done:
        return progress
    observer_ids: dict[str, int] = {}
    total = (
        await (await session.run("MATCH (u:NostrUser) RETURN count(u) AS n")).single()
    )["n"]
    started, nodes_at_start, copied = time.monotonic(), progress.nodes, 0
    while True:
        nodes = await _read_page(session, progress.after, page_size)
        if not nodes:
            break
        page = await build_page(nodes, observer_ids)
        progress.after = nodes[-1]["pubkey"]
        progress.nodes += len(nodes)
        progress.rows_without_user_id += page.rows_without_user_id
        progress.non_finite += page.non_finite
        async with scorecard_session() as db:
            if page.rows:
                await _copy_rows(db, page.rows)
            await db.execute(
                text(
                    "UPDATE score_copy_progress SET after = :after, nodes = :nodes,"
                    " rows_without_user_id = :skipped, non_finite = :non_finite"
                ),
                {
                    "after": progress.after,
                    "nodes": progress.nodes,
                    "skipped": progress.rows_without_user_id,
                    "non_finite": progress.non_finite,
                },
            )
        copied += len(page.rows)
        elapsed = max(time.monotonic() - started, 1e-9)
        rate = (progress.nodes - nodes_at_start) / elapsed
        left = max(total - progress.nodes, 0) / max(rate, 1e-9)
        log(
            f"  {progress.nodes}/{total} nodes, {copied} rows this run,"
            f" {rate:.0f} nodes/s, about {left / 60:.1f} min left"
        )
    async with scorecard_session() as db:
        await db.execute(text("UPDATE score_copy_progress SET done = true"))
    progress.done = True
    return progress


async def copy_one(session, observer: str, page_size: int, log=print) -> int:
    """Redo one Observer: read all its scores, then replace its rows in one transaction."""
    await ensure_state()
    observer_ids: dict[str, int] = {}
    rows: list[tuple] = []
    after = ""
    while True:
        nodes = await _read_page(session, after, page_size)
        if not nodes:
            break
        rows.extend((await build_page(nodes, observer_ids, only=observer)).rows)
        after = nodes[-1]["pubkey"]
    async with scorecard_session() as db:
        observer_id = (await ids_for([observer]))[observer]
        await db.execute(
            text("DELETE FROM scorecard WHERE observer_id = :o"), {"o": observer_id}
        )
        await db.execute(
            text("DELETE FROM score_copy_checksum WHERE observer_id = :o"),
            {"o": observer_id},
        )
        if rows:
            await _copy_rows(db, rows)
    log(f"  {observer}: {len(rows)} rows")
    return len(rows)


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


async def mismatches() -> list[str]:
    """Observers whose rows in `scorecard` are not what the sweep read, by pubkey."""
    async with scorecard_session() as db:
        rows = (await db.execute(text(_COMPARE))).all()
    bad = []
    for pubkey, rows_read, rows_stored, *sums in rows:
        pairs = zip(sums[0::2], sums[1::2])
        if rows_stored != rows_read or not all(
            _close(read, stored) for read, stored in pairs
        ):
            bad.append(
                f"{pubkey}: read {rows_read} rows {sums[0::2]},"
                f" stored {rows_stored} rows {sums[1::2]}"
            )
    return bad


async def runs_in_pipeline() -> int:
    async with db_session() as db:
        result = await db.execute(
            select(func.count())
            .select_from(BrainstormRequest)
            .where(in_pipeline_condition())
        )
        return int(result.scalar_one())


async def _print_status(session) -> None:
    await ensure_state()
    progress = await read_progress()
    keys = await session.run(
        "CALL db.propertyKeys() YIELD propertyKey WITH propertyKey"
        " WHERE propertyKey STARTS WITH 'influence_' RETURN count(*) AS n"
    )
    total = await session.run("MATCH (u:NostrUser) RETURN count(u) AS n")
    async with scorecard_session() as db:
        copied = (
            await db.execute(
                text("SELECT count(*), coalesce(sum(rows), 0) FROM score_copy_checksum")
            )
        ).one()
    print(f"Observers ever stored in Neo4j: {(await keys.single())['n']}")
    print(f"user nodes read: {progress.nodes} of {(await total.single())['n']}")
    print(f"Observers copied: {copied[0]}, rows: {copied[1]}")
    print(
        f"scores on nodes without a user_id (not copied): {progress.rows_without_user_id}"
    )
    print(f"NaN or infinite Influence (stored as none): {progress.non_finite}")
    print(f"sweep finished: {'yes' if progress.done else 'no'}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--only", metavar="OBSERVER", help="redo one Observer's rows")
    ap.add_argument("--restart", action="store_true", help="forget progress")
    ap.add_argument(
        "--force", action="store_true", help="run with runs in the pipeline"
    )
    ap.add_argument("--page", type=int, default=200, help="user nodes per read")
    args = ap.parse_args()

    if not settings.scorecard_db_url:
        print("SCORECARD_DB_URL is not set")
        return 2
    if args.only and not _OBSERVER.fullmatch(args.only):
        print("--only takes a 64-char lowercase hex pubkey")
        return 2

    started = time.monotonic()
    try:
        async with neo4j_driver.session() as session:
            if args.status:
                await _print_status(session)
                return 0

            if args.verify:
                await ensure_state()
                progress = await read_progress()
                bad = await mismatches()
                for line in bad[:50]:
                    print(f"  {line}")
                print(f"Observers that do not match: {len(bad)}")
                print(
                    f"scores not copied (node has no user_id): {progress.rows_without_user_id}"
                )
                print(f"sweep finished: {'yes' if progress.done else 'no'}")
                ok = progress.done and not bad and not progress.rows_without_user_id
                return 0 if ok else 1

            in_pipeline = await runs_in_pipeline()
            if in_pipeline and not args.force:
                print(
                    f"{in_pipeline} runs are in the pipeline: pause the scheduler and"
                    " let them finish, or pass --force"
                )
                return 2

            if not args.apply:
                await _print_status(session)
                print("[dry-run] no writes; pass --apply to copy")
                return 0

            if args.restart:
                await ensure_state()
                async with scorecard_session() as db:
                    await db.execute(text("TRUNCATE score_copy_checksum"))
                    await db.execute(
                        text(
                            "UPDATE score_copy_progress SET after = '', nodes = 0,"
                            " rows_without_user_id = 0, non_finite = 0, done = false"
                        )
                    )
                print("progress forgotten; scorecard rows left in place")
                return 0

            if args.only:
                await copy_one(session, args.only, args.page)
            else:
                progress = await copy_all(session, args.page)
                print(f"read {progress.nodes} nodes")
            print(f"done in {time.monotonic() - started:.1f}s; now run --verify")
            return 0
    finally:
        await dispose_scorecard_engine()
        await neo4j_driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
