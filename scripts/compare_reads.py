"""Record what the read endpoints would answer for a set of Observers, and diff two records.

For the `SCORES_IN_POSTGRES` cutover: record A before the flip, record B after,
with ingest held (`ingest:hold`). Reads in-process through the services the
routers call, with the Observer passed explicitly; runs in a server pod.
See scripts/CLAUDE.md.

    poetry run python -m scripts.compare_reads record --sample 20 --out A
    poetry run python -m scripts.compare_reads record --observers A/observers.txt --out B
    poetry run python -m scripts.compare_reads diff A B [--reread]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KINDS = [
    "followed_by",
    "following",
    "muted_by",
    "muting",
    "reported_by",
    "reporting",
    "flagged",
]
TIERS = [
    "high",
    "medium_high",
    "medium",
    "medium_low",
    "low",
    "low_and_reported_by_2_or_more_trusted_pubkeys",
]
PAGE_LIMIT = 50
SHORTEST_PATH_MAX_HOPS = 30  # the route's default
PAGES = 3
LOOKUPS = 3
VOLATILE = {
    "timestamp",
    "request_id",
    "requestId",
    "created_at",
    "createdAt",
    "updated_at",
    "updatedAt",
    "generated_at",
    "generatedAt",
    "computed_at",
    "computedAt",
    "as_of",
    "asOf",
}
# Lists that are sets: compared sorted.
UNORDERED = {"pubkeys"}

# (observer, endpoint, params) -> (status, body as the API would send it)
Fetch = Callable[[str, str, dict], Awaitable[tuple[int, bytes]]]


@dataclass(frozen=True)
class Call:
    endpoint: str
    observer: str
    params: tuple[tuple[str, str], ...]

    @property
    def key(self) -> str:
        return f"{self.endpoint}|{self.observer}|{json.dumps(self.params)}"

    @property
    def file(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()[:24]


@dataclass
class Entry:
    endpoint: str
    observer: str
    params: list
    status: int
    hash: str
    count: int | None

    @property
    def call(self) -> Call:
        return Call(self.endpoint, self.observer, tuple(tuple(p) for p in self.params))


# ----------------- normalise / hash -----------------


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ties_in_order(items: list) -> list:
    """Within a run of equal `influence`, a stable order by content."""
    out, run = [], []
    for item in items:
        if run and item.get("influence") != run[0].get("influence"):
            out += sorted(run, key=_canonical)
            run = []
        run.append(item)
    return out + sorted(run, key=_canonical)


def normalise(value, key: str | None = None):
    if isinstance(value, dict):
        return {k: normalise(v, k) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        items = [normalise(v) for v in value]
        if key in UNORDERED:
            return sorted(items, key=_canonical)
        if items and all(isinstance(i, dict) and "influence" in i for i in items):
            return _ties_in_order(items)
        return items
    return value


def item_count(body) -> int | None:
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for name in ("items", "pubkeys", "results", "layers"):
            if isinstance(data.get(name), list):
                return len(data[name])
    return None


def digest(status: int, raw: bytes) -> tuple[str, int | None, object]:
    """(hash, item count, parsed body) of one answer."""
    try:
        body = json.loads(raw)
    except ValueError:
        return hashlib.sha256(raw).hexdigest(), None, None
    text = _canonical({"status": status, "body": normalise(body)})
    return hashlib.sha256(text.encode()).hexdigest(), item_count(body), body


# ----------------- record -----------------


def _connections_calls(observer: str) -> list[dict]:
    """First call of each paged walk; later pages follow `next_cursor`."""
    out = []
    for kind in KINDS:
        for order in ("desc", "asc"):
            out.append({"kind": kind, "order": order, "with_total": "true"})
        if kind == "flagged":
            continue
        out.append({"kind": kind, "verified_only": "true"})
        for tier in TIERS:
            out.append({"kind": kind, "tier": tier})
    return out


class Recorder:
    def __init__(self, fetch: Fetch, raw_dir: Path | None = None):
        self.fetch = fetch
        self.raw_dir = raw_dir
        self.entries: list[Entry] = []

    async def get(self, endpoint: str, observer: str, params: dict) -> object:
        call = Call(endpoint, observer, tuple(sorted(params.items())))
        status, raw = await self.fetch(observer, endpoint, dict(call.params))
        hashed, count, body = digest(status, raw)
        self.entries.append(
            Entry(
                endpoint,
                observer,
                [list(p) for p in call.params],
                status,
                hashed,
                count,
            )
        )
        if self.raw_dir is not None:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            (self.raw_dir / f"{call.file}.json").write_bytes(raw)
        return body

    async def observer(self, observer: str) -> None:
        await self.get("overview", observer, {"pubkey": observer})
        await self.get("stats", observer, {"pubkey": observer})
        await self.get("whitelist", observer, {})
        await self.get("network_alerts", observer, {})
        subjects: list[str] = []
        for params in _connections_calls(observer):
            cursor = None
            for _ in range(PAGES):
                page = dict(params, pubkey=observer, limit=str(PAGE_LIMIT))
                if cursor:
                    page["cursor"] = cursor
                body = await self.get("connections", observer, page)
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict):
                    break
                if params["kind"] == "following" and params.get("order") == "desc":
                    subjects += [i["pubkey"] for i in data.get("items", [])][:LOOKUPS]
                cursor = data.get("next_cursor")
                if not cursor:
                    break
        for subject in subjects[:LOOKUPS]:
            await self.get("overview", observer, {"pubkey": subject})
            await self.get("shortest_path", observer, {"to": subject})


async def record(fetch: Fetch, observers: list[str], out: Path, raw: bool, log=print):
    out.mkdir(parents=True, exist_ok=True)
    (out / "observers.txt").write_text("".join(f"{o}\n" for o in observers))
    recorder = Recorder(fetch, out / "raw" if raw else None)
    for i, observer in enumerate(observers, 1):
        await recorder.observer(observer)
        log(f"  {i}/{len(observers)} {observer[:12]}… {len(recorder.entries)} calls")
    with (out / "calls.jsonl").open("w") as fh:
        for entry in recorder.entries:
            fh.write(json.dumps(asdict(entry)) + "\n")
    return recorder.entries


# ----------------- diff -----------------


def load(directory: Path) -> dict[str, Entry]:
    entries = {}
    with (directory / "calls.jsonl").open() as fh:
        for line in fh:
            entry = Entry(**json.loads(line))
            entries[entry.call.key] = entry
    return entries


@dataclass
class Difference:
    key: str
    a: Entry | None
    b: Entry | None


def differences(a: dict[str, Entry], b: dict[str, Entry]) -> list[Difference]:
    out = []
    for key in sorted(a.keys() | b.keys()):
        ea, eb = a.get(key), b.get(key)
        if ea is None or eb is None or (ea.status, ea.hash) != (eb.status, eb.hash):
            out.append(Difference(key, ea, eb))
    return out


async def save_mismatches(
    found: list[Difference], a_dir: Path, b_dir: Path, fetch: Fetch | None
) -> Path:
    """Both raw bodies per mismatched call: A's and B's if they were recorded
    with --raw, and a re-read of the current answer when `fetch` is given."""
    out = b_dir / "mismatches"
    out.mkdir(parents=True, exist_ok=True)
    for d in found:
        entry = d.b or d.a
        assert entry is not None
        call = entry.call
        for side, directory in (("a", a_dir), ("b", b_dir)):
            src = directory / "raw" / f"{call.file}.json"
            if src.exists():
                (out / f"{call.file}.{side}.json").write_bytes(src.read_bytes())
        if fetch is not None:
            status, raw = await fetch(call.observer, call.endpoint, dict(call.params))
            (out / f"{call.file}.now.json").write_bytes(raw)
            now_hash = digest(status, raw)[0]
            note = {
                "call": call.key,
                "a": d.a and d.a.hash,
                "b": d.b and d.b.hash,
                "now": now_hash,
            }
            (out / f"{call.file}.call.json").write_text(json.dumps(note, indent=1))
    return out


# ----------------- Observer selection -----------------


def choose_observers(
    house: str,
    sizes: dict[str, int],
    newest: list[str],
    free: list[str],
    paid: list[str],
    n: int = 20,
) -> list[str]:
    """House, the 3 largest, the newest, 2 Free and 2 paid near the median, and
    the rest spread over the size range. Only Observers with stored scores."""
    scored = sorted((o for o in sizes if sizes[o] > 0), key=lambda o: (-sizes[o], o))
    chosen: list[str] = []

    def take(candidates, k):
        for o in candidates:
            if k <= 0 or len(chosen) >= n:
                return
            if o not in chosen and (o == house or o in sizes):
                chosen.append(o)
                k -= 1

    take([house], 1)
    take(scored, 3)
    take([o for o in newest if sizes.get(o, 0) > 0], 1)
    if scored:
        median = statistics.median(sizes[o] for o in scored)

        def near(group):
            return sorted(
                (o for o in group if sizes.get(o, 0) > 0),
                key=lambda o: (abs(sizes[o] - median), o),
            )

        take(near(free), 2)
        take(near(paid), 2)
        rest = [o for o in scored if o not in chosen]
        left = n - len(chosen)
        if rest and left > 0:
            step = max(len(rest) / left, 1)
            take([rest[int(i * step)] for i in range(min(left, len(rest)))], left)
    return chosen


async def sample_from_databases(n: int) -> list[str]:
    """Needs the server's env: the domain database and the Scorecard database."""
    from sqlalchemy import text

    from app.core.database import db_session
    from app.core.scorecard_database import scorecard_session
    from app.utils.observer import default_observer_pubkey

    async with scorecard_session() as db:
        rows = await db.execute(
            text(
                "SELECT u.pubkey, s.n FROM (SELECT observer_id, count(*) AS n"
                " FROM scorecard GROUP BY observer_id) s"
                " JOIN nostr_user u ON u.id = s.observer_id"
            )
        )
        sizes = {pubkey: n for pubkey, n in rows}
    async with db_session() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT pubkey, scheduling_id IS NOT NULL FROM brainstorm_nsec"
                    " ORDER BY created_at DESC"
                )
            )
        ).all()
    newest = [pubkey for pubkey, _ in rows]
    paid = [pubkey for pubkey, assigned in rows if assigned]
    free = [pubkey for pubkey, assigned in rows if not assigned]
    return choose_observers(default_observer_pubkey(), sizes, newest, free, paid, n)


# ----------------- in-process reads -----------------


def _serialised(response) -> bytes:
    """The body FastAPI would send for this response model."""
    return json.dumps(response.model_dump(mode="json", by_alias=True)).encode()


async def _read(endpoint: str, observer: str, p: dict):
    """What the router answers, from the same services, as `observer`."""
    from app.core.database import db_session
    from app.schemas import request_response_schemas as r
    from app.services import graph_service, network_alerts_service
    from app.services.user_service import (
        get_user_connections,
        get_user_overview,
        get_user_stats,
        get_whitelisted_pubkeys_of_observer,
        whitelist_updated_at,
    )
    from app.services.verified_cutoffs import resolve_verified_cutoffs

    async with db_session() as db:
        cutoffs = await resolve_verified_cutoffs(db, observer)
        if endpoint == "whitelist":
            if await whitelist_updated_at(db, observer) is None:
                pubkeys = []
            else:
                pubkeys = await get_whitelisted_pubkeys_of_observer(db, observer)
            return r.GetWhitelistedPubkeysOfObserverResponse(
                data=r.WhitelistedPubkeys(
                    observerPubkey=observer, numPubkeys=len(pubkeys), pubkeys=pubkeys
                )
            )
    if endpoint == "overview":
        data = await get_user_overview(
            pubkey=p["pubkey"],
            observer=observer,
            verified_line=cutoffs.verified_line,
            reporter_cutoff=cutoffs.reporter,
        )
        return r.GetUserOverviewResponse(data=data)
    if endpoint == "stats":
        data = await get_user_stats(
            pubkey=p["pubkey"], observer=observer, cutoffs=cutoffs
        )
        return r.GetUserStatsResponse(data=data)
    if endpoint == "connections":
        data = await get_user_connections(
            pubkey=p["pubkey"],
            observer=observer,
            cutoffs=cutoffs,
            kind=p["kind"],
            limit=int(p["limit"]),
            cursor=p.get("cursor"),
            order=p.get("order", "desc"),
            tier=p.get("tier"),
            verified_only=p.get("verified_only") == "true",
            with_total=p.get("with_total") == "true",
        )
        return r.GetUserConnectionsResponse(data=data)
    if endpoint == "network_alerts":
        data = await network_alerts_service.get_network_alerts_for_observer(
            observer=observer, cutoffs=cutoffs
        )
        return r.GetNetworkAlertsResponse(data=data)
    if endpoint == "shortest_path":
        data = await graph_service.get_shortest_follow_path(
            observer, p["to"], SHORTEST_PATH_MAX_HOPS
        )
        return r.GetShortestPathResponse(data=data)
    raise ValueError(f"unknown endpoint {endpoint}")


def service_fetcher(read=_read) -> Fetch:
    """Each read as (status, body); an HTTPException becomes its status and detail."""
    from fastapi import HTTPException

    async def fetch(observer: str, endpoint: str, params: dict) -> tuple[int, bytes]:
        try:
            return 200, _serialised(await read(endpoint, observer, params))
        except HTTPException as e:
            return e.status_code, json.dumps({"detail": e.detail}).encode()

    return fetch


def _read_observers(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


async def _with_services(work):
    os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")
    from app.core.database import engine
    from app.core.scorecard_database import (
        dispose_scorecard_engine,
        unbounded_statements,
    )
    from app.neo4j_db.driver import driver as neo4j_driver

    unbounded_statements()
    try:
        return await work()
    finally:
        await dispose_scorecard_engine()
        await engine.dispose()
        await neo4j_driver.close()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    who = rec.add_mutually_exclusive_group(required=True)
    who.add_argument("--observers", type=Path, help="one pubkey per line")
    who.add_argument("--sample", type=int, metavar="N", help="choose N from the DBs")
    rec.add_argument("--out", type=Path, required=True)
    rec.add_argument("--raw", action="store_true", help="also keep raw bodies")
    dif = sub.add_parser("diff")
    dif.add_argument("a", type=Path)
    dif.add_argument("b", type=Path)
    dif.add_argument(
        "--reread", action="store_true", help="re-read mismatched calls now"
    )
    args = ap.parse_args()

    if args.cmd == "record":

        async def work():
            if args.sample:
                observers = await sample_from_databases(args.sample)
            else:
                observers = _read_observers(args.observers)
            entries = await record(service_fetcher(), observers, args.out, args.raw)
            print(f"{len(entries)} calls for {len(observers)} Observers in {args.out}")
            return 0

        return await _with_services(work)

    found = differences(load(args.a), load(args.b))
    for d in found[:50]:
        print(f"  {d.key}: a {d.a and d.a.hash[:12]}, b {d.b and d.b.hash[:12]}")
    print(f"calls that differ: {len(found)}")
    if not found:
        return 0
    if args.reread:
        where = await _with_services(
            lambda: save_mismatches(found, args.a, args.b, service_fetcher())
        )
    else:
        where = await save_mismatches(found, args.a, args.b, None)
    print(f"raw bodies in {where}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
