"""Copy every Observer's stored scores from Neo4j properties into the Scorecard database.

Reads Neo4j and never writes it. Dry-run by default. See scripts/CLAUDE.md.

    poetry run python -m scripts.copy_scores_to_postgres                     # what it would do
    poetry run python -m scripts.copy_scores_to_postgres --apply [--page N]  # copy; resumes
    poetry run python -m scripts.copy_scores_to_postgres --status
    poetry run python -m scripts.copy_scores_to_postgres --verify [--only O] # exit 1 on a mismatch
    poetry run python -m scripts.copy_scores_to_postgres --only <observer> --apply
    poetry run python -m scripts.copy_scores_to_postgres --catch-up --apply  # Observers run since
    poetry run python -m scripts.copy_scores_to_postgres --restart --apply   # forget progress
"""
from __future__ import annotations

import argparse
import asyncio
import heapq
import math
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from sqlalchemy import func, select, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import db_session  # noqa: E402
from app.core.scorecard_database import (  # noqa: E402
    dispose_scorecard_engine,
    scorecard_session,
    unbounded_statements,
)
from app.db_models import BrainstormRequest  # noqa: E402
from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402
from app.repos.brainstorm_request_repo import in_pipeline_condition  # noqa: E402
from app.repos.user_id_repo import ids_for  # noqa: E402
from app.utils.datetimes import as_naive_utc, utc_now  # noqa: E402

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
# The same walk, but only the named Observers' scores leave the database.
_PAGE_OF = """
MATCH (u:NostrUser) WHERE u.pubkey > $after
WITH u ORDER BY u.pubkey LIMIT $limit
WITH u, properties(u) AS p
RETURN u.pubkey AS pubkey, p.user_id AS user_id,
       [k IN $keys WHERE p[k] IS NOT NULL | [k, p[k]]] AS found
"""
# By user_id, so the walk meets the Scorecard rows in their key order.
_PAGE_OF_BY_ID = """
MATCH (u:NostrUser) WHERE u.user_id > $after
WITH u ORDER BY u.user_id LIMIT $limit
WITH u, properties(u) AS p
RETURN u.user_id AS user_id,
       [k IN $keys WHERE p[k] IS NOT NULL | [k, p[k]]] AS found
"""
_SCORED_WITHOUT_ID = """
MATCH (u:NostrUser) WHERE u.user_id IS NULL
WITH properties(u) AS p
RETURN sum(size([o IN $observers WHERE p['influence_' + o] IS NOT NULL
    OR p['hops_' + o] IS NOT NULL OR p['trusted_followers_' + o] IS NOT NULL])) AS n
"""
_PAGE_OF_FACTOR = 25  # little crosses the wire, so pages can be larger
# Covers clock skew between this host and the domain database.
_CATCH_UP_MARGIN = timedelta(minutes=10)

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
    "ALTER TABLE score_copy_progress ADD COLUMN IF NOT EXISTS started_at timestamp",
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
    # When the last complete read of Neo4j began (UTC): a run finished after it
    # may not be in `scorecard`.
    started_at: datetime | None = None


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


async def _read_page_of(
    session, observers: list[str], after: str, limit: int
) -> list[dict]:
    keys = [f"{prefix}_{o}" for o in observers for prefix in FIELDS]

    async def work(tx):
        result = await tx.run(_PAGE_OF, after=after, limit=limit, keys=keys)
        return [
            {
                "pubkey": r["pubkey"],
                "props": {**dict(r["found"]), "user_id": r["user_id"]},
            }
            async for r in result
        ]

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
                    "SELECT after, nodes, rows_without_user_id, non_finite, done,"
                    " started_at"
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
    if not progress.nodes:
        progress.started_at = utc_now()
        async with scorecard_session() as db:
            await db.execute(
                text("UPDATE score_copy_progress SET started_at = :at"),
                {"at": progress.started_at},
            )
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
        nodes = await _read_page_of(
            session, [observer], after, page_size * _PAGE_OF_FACTOR
        )
        if not nodes:
            break
        rows.extend((await build_page(nodes, observer_ids)).rows)
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


async def observers_run_since(since: datetime) -> list[str]:
    """Observers with a request created or updated at or after `since` (UTC)."""
    async with db_session() as db:
        rows = (
            await db.execute(
                select(
                    BrainstormRequest.pubkey,
                    func.max(BrainstormRequest.created_at),
                    func.max(BrainstormRequest.updated_at),
                )
                .where(BrainstormRequest.pubkey.is_not(None))
                .group_by(BrainstormRequest.pubkey)
            )
        ).all()
    # Compared here, not in SQL: production holds these columns with a time zone.
    return sorted(
        pubkey
        for pubkey, *stamps in rows
        if _OBSERVER.fullmatch(pubkey)
        and max(as_naive_utc(t) for t in stamps if t is not None) >= since
    )


async def catch_up(
    session, since: datetime, page_size: int, apply: bool = True, log=print
) -> list[str]:
    """Redo the Observers run since `since`, in one walk of the user nodes.

    Their rows are deleted first and come back page by page: run it before
    anything reads `scorecard`. An interrupted catch-up is simply run again.
    """
    await ensure_state()
    began = utc_now()
    observers = await observers_run_since(since - _CATCH_UP_MARGIN)
    log(f"{len(observers)} Observers run since {since.isoformat()}")
    if not apply:
        return observers
    if observers:
        observer_ids = await ids_for(observers)
        async with scorecard_session() as db:
            for table in ("scorecard", "score_copy_checksum"):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE observer_id = ANY(:ids)"),
                    {"ids": list(observer_ids.values())},
                )
        after, rows, skipped = "", 0, 0
        while True:
            nodes = await _read_page_of(
                session, observers, after, page_size * _PAGE_OF_FACTOR
            )
            if not nodes:
                break
            page = await build_page(nodes, observer_ids)
            after = nodes[-1]["pubkey"]
            if page.rows:
                async with scorecard_session() as db:
                    await _copy_rows(db, page.rows)
            rows += len(page.rows)
            skipped += page.rows_without_user_id
        log(f"  {rows} rows; {skipped} on nodes without a user_id (not copied)")
    async with scorecard_session() as db:
        await db.execute(
            text("UPDATE score_copy_progress SET started_at = :at"), {"at": began}
        )
    return observers


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


# ----------------- per-row verify -----------------

_STORED_PAGE = 10_000
_SAMPLES = 5


@dataclass
class RowCheck:
    """Per-row comparison of Neo4j against `scorecard`, by Observer pubkey."""

    observers: int = 0
    rows: int = 0
    rows_without_user_id: int = 0
    bad: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)

    def mismatch(self, observer: str, what: str) -> None:
        self.bad[observer] = self.bad.get(observer, 0) + 1
        seen = self.samples.setdefault(observer, [])
        if len(seen) < _SAMPLES:
            seen.append(what)

    @property
    def ok(self) -> bool:
        return not self.bad and not self.rows_without_user_id


async def compare_rows(
    expected: AsyncIterator[tuple[int, dict[int, tuple]]],
    stored: dict[int, AsyncIterator[tuple]],
    names: dict[int, str],
    check: RowCheck,
) -> None:
    """Merge Neo4j's rows (per observee id, ascending: {observer_id: (influence,
    hops, followers)}) with each Observer's stored rows (observee_id, influence,
    hops, followers), ascending. Memory: one row per Observer plus one node."""
    heads: list[tuple[int, int, tuple]] = []

    async def advance(observer_id: int) -> None:
        row = await anext(stored[observer_id], None)
        if row is not None:
            heapq.heappush(heads, (row[0], observer_id, tuple(row[1:])))

    for observer_id in stored:
        await advance(observer_id)

    async def extras_below(limit: float) -> None:
        while heads and heads[0][0] < limit:
            observee, observer_id, _ = heapq.heappop(heads)
            check.mismatch(names[observer_id], f"{observee}: only in scorecard")
            await advance(observer_id)

    async for observee, rows in expected:
        await extras_below(observee)
        rows = dict(rows)
        check.rows += len(rows)
        while heads and heads[0][0] == observee:
            _, observer_id, got = heapq.heappop(heads)
            want = rows.pop(observer_id, None)
            if want is None:
                check.mismatch(names[observer_id], f"{observee}: only in scorecard")
            elif want != got:
                check.mismatch(
                    names[observer_id], f"{observee}: neo4j {want}, scorecard {got}"
                )
            await advance(observer_id)
        for observer_id in rows:
            check.mismatch(names[observer_id], f"{observee}: missing from scorecard")
    await extras_below(math.inf)


async def _stored_rows(observer_id: int) -> AsyncIterator[tuple]:
    after = -1
    while True:
        async with scorecard_session() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT observee_id, influence, hops, verified_followers"
                        " FROM scorecard WHERE observer_id = :o AND observee_id > :after"
                        " ORDER BY observee_id LIMIT :n"
                    ),
                    {"o": observer_id, "after": after, "n": _STORED_PAGE},
                )
            ).all()
        for row in rows:
            yield tuple(row)
        if len(rows) < _STORED_PAGE:
            return
        after = rows[-1][0]


async def _expected_rows(
    session, observers: list[str], observer_ids: dict[str, int], page_size: int
) -> AsyncIterator[tuple[int, dict[int, tuple]]]:
    keys = [f"{prefix}_{o}" for o in observers for prefix in FIELDS]
    after = -1
    while True:

        async def work(tx, after=after):
            result = await tx.run(
                _PAGE_OF_BY_ID, after=after, limit=page_size, keys=keys
            )
            return [
                {"props": {**dict(r["found"]), "user_id": r["user_id"]}}
                async for r in result
            ]

        nodes = await session.execute_read(work)
        if not nodes:
            return
        page = await build_page(nodes, observer_ids)
        by_node: dict[int, dict[int, tuple]] = {}
        for observer_id, observee, *values in page.rows:
            by_node.setdefault(observee, {})[observer_id] = tuple(values)
        for observee in sorted(by_node):
            yield observee, by_node[observee]
        after = nodes[-1]["props"]["user_id"]


async def _scored_without_id(session, observers: list[str]) -> int:
    async def work(tx):
        record = await (await tx.run(_SCORED_WITHOUT_ID, observers=observers)).single()
        return record["n"] or 0

    return await session.execute_read(work)


async def _observers_to_verify(session) -> list[str]:
    """Every Observer Neo4j has a score key for, and every one with stored rows."""
    result = await session.run(
        "CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey"
    )
    found: set[str] = set()
    async for record in result:
        key = record["propertyKey"]
        if len(key) > _SUFFIX and key[:-_SUFFIX] in FIELDS:
            if _OBSERVER.fullmatch(key[-64:]):
                found.add(key[-64:])
    async with scorecard_session() as db:
        rows = await db.execute(
            text(
                "SELECT u.pubkey FROM nostr_user u WHERE u.id IN ("
                "SELECT observer_id FROM scorecard_observer"
                " UNION SELECT observer_id FROM score_copy_checksum)"
            )
        )
        found.update(pubkey for (pubkey,) in rows)
    return sorted(found)


async def verify_rows(
    session,
    observers: list[str] | None,
    page_size: int,
    group: int = 50,
    log=print,
) -> RowCheck:
    """Compare every stored row with Neo4j, `group` Observers per walk of the nodes."""
    if observers is None:
        observers = await _observers_to_verify(session)
    check = RowCheck(observers=len(observers))
    for start in range(0, len(observers), group):
        chunk = observers[start : start + group]
        ids = await ids_for(chunk, create=False)
        # An Observer without an id has no rows; any score Neo4j holds is missing.
        observer_ids = {o: ids.get(o, -1 - i) for i, o in enumerate(chunk)}
        names = {i: o for o, i in observer_ids.items()}
        stored = {i: _stored_rows(i) for o, i in observer_ids.items() if o in ids}
        await compare_rows(
            _expected_rows(session, chunk, observer_ids, page_size),
            stored,
            names,
            check,
        )
        check.rows_without_user_id += await _scored_without_id(session, chunk)
        log(
            f"  {min(start + group, len(observers))}/{len(observers)} Observers,"
            f" {check.rows} rows, {len(check.bad)} Observers with a mismatch"
        )
    return check


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
    if progress.done and progress.started_at:
        waiting = await observers_run_since(progress.started_at - _CATCH_UP_MARGIN)
        print(
            f"Observers run since the last read began ({progress.started_at.isoformat()}):"
            f" {len(waiting)}"
        )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--only", metavar="OBSERVER", help="redo one Observer's rows")
    ap.add_argument("--restart", action="store_true", help="forget progress")
    ap.add_argument(
        "--catch-up",
        action="store_true",
        help="redo the Observers run since the last read began",
    )
    ap.add_argument(
        "--since",
        type=datetime.fromisoformat,
        help="with --catch-up: a UTC time instead of the recorded one",
    )
    ap.add_argument(
        "--force", action="store_true", help="run with runs in the pipeline"
    )
    ap.add_argument("--page", type=int, default=200, help="user nodes per read")
    ap.add_argument(
        "--group", type=int, default=50, help="with --verify: Observers per walk"
    )
    args = ap.parse_args()
    unbounded_statements()

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
                observers = [args.only] if args.only else None
                check = await verify_rows(
                    session, observers, args.page * _PAGE_OF_FACTOR, args.group
                )
                for observer, n in sorted(check.bad.items())[:50]:
                    print(f"  {observer}: {n} rows differ")
                    for line in check.samples[observer]:
                        print(f"    {line}")
                print(f"Observers checked: {check.observers}, rows: {check.rows}")
                print(f"Observers that do not match: {len(check.bad)}")
                print(
                    f"scores not copied (node has no user_id): {check.rows_without_user_id}"
                )
                print(f"sweep finished: {'yes' if progress.done else 'no'}")
                return 0 if progress.done and check.ok else 1

            in_pipeline = await runs_in_pipeline()
            if in_pipeline and not args.force:
                print(
                    f"{in_pipeline} runs are in the pipeline: pause the scheduler and"
                    " let them finish, or pass --force"
                )
                return 2

            if args.catch_up:
                await ensure_state()
                progress = await read_progress()
                since = as_naive_utc(args.since) or progress.started_at
                if not progress.done or since is None:
                    print("finish the sweep first (or pass --since)")
                    return 2
                observers = await catch_up(session, since, args.page, args.apply)
                for observer in observers:
                    print(f"  {observer}")
                if not args.apply:
                    print("[dry-run] no writes; pass --apply to redo them")
                    return 0
                print(f"done in {time.monotonic() - started:.1f}s; now run --verify")
                return 0

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
                            " rows_without_user_id = 0, non_finite = 0, done = false,"
                            " started_at = NULL"
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
