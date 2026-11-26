"""Rebuild the Neo4j store without score properties: export, then check the import.

Reads the source database and never writes it. See scripts/CLAUDE.md for the
whole procedure; the import itself is `neo4j-admin`, run by the operator.

    poetry run python -m scripts.rebuild_neo4j inventory
    poetry run python -m scripts.rebuild_neo4j export --out DIR
    # ... neo4j-admin database import full (the command is written to DIR/import-command.txt)
    # then, with NEO4J_DB_URL pointing at the rebuilt database:
    poetry run python -m scripts.rebuild_neo4j schema --dir DIR [--apply]
    poetry run python -m scripts.rebuild_neo4j check --dir DIR
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")

from neo4j.time import Date, DateTime  # noqa: E402

from app.neo4j_db.driver import driver as neo4j_driver  # noqa: E402

# Everything the rebuilt store keeps is listed here. A label, relationship type
# or property outside these lists stops the export until someone decides.
KEPT_LABELS = {"NostrUser", "GraphStreamerState", "GraphStreamerRedisPending"}
KEPT_RELATIONSHIPS = {"FOLLOWS", "MUTES", "REPORTS", "DESIGNATES", "ZAPS_VIA"}
KEPT_USER_KEYS = {
    "pubkey",
    "user_id",
    # brainstorm_profile_resolver
    "nip05",
    "nip05_status",
    "nip05_checked_at",
    "lnurl",
    "lnurl_status",
    # graph-streamer bookkeeping
    "follows_event",
    "follows_at",
    "mutes_event",
    "mutes_at",
    "designations_event",
    "designations_at",
    "report_events",
    "redis_pending_removed",
}
# Labels whose nodes keep every property (graph-streamer's one state node).
KEEP_ALL_KEYS_ON = {"GraphStreamerState"}
DROPPED_PREFIXES = ("influence_", "hops_", "trusted_followers_", "trusted_reporters_")

ARRAY_DELIMITER = ";"
SAMPLE_SIZE = 500

_IS_DROPPED = "(" + " OR ".join(f"k STARTS WITH '{p}'" for p in DROPPED_PREFIXES) + ")"
# One read of each node's property map; the scores are filtered out in the
# database, so they never cross the wire.
_NODES = f"""
MATCH (n) WHERE id(n) IN range($lo, $hi - 1)
WITH n, properties(n) AS p
RETURN id(n) AS id, labels(n) AS labels,
       [k IN keys(p) WHERE NOT {_IS_DROPPED} | [k, p[k]]] AS kept,
       size([k IN keys(p) WHERE {_IS_DROPPED}]) AS dropped
"""
_RELATIONSHIPS = """
MATCH (a) WHERE id(a) IN range($lo, $hi - 1)
MATCH (a)-[r]->(b)
RETURN id(a) AS start, id(b) AS end, type(r) AS type, properties(r) AS props
"""
_DEGREES = """
MATCH (n:NostrUser) WHERE n.pubkey IN $pubkeys
RETURN n.pubkey AS pubkey,
       [t IN $types | COUNT { (n)-[r]->() WHERE type(r) = t }] AS out_degrees,
       [t IN $types | COUNT { (n)<-[r]-() WHERE type(r) = t }] AS in_degrees
"""


class Stop(RuntimeError):
    """Something the rebuild has no rule for; nothing is guessed."""


def is_dropped(key: str) -> bool:
    return key.startswith(DROPPED_PREFIXES)


def csv_type(value) -> str:
    """The neo4j-admin column type of one property value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, DateTime):
        return "datetime"
    if isinstance(value, Date):
        return "date"
    if isinstance(value, list):
        inner = {csv_type(v) for v in value}
        if len(inner) > 1 and inner <= {"long", "double"}:
            inner = {"double"}
        if len(inner) != 1 or next(iter(inner)).endswith("[]"):
            raise Stop(f"an array of mixed or nested values: {value!r:.80}")
        return next(iter(inner)) + "[]"
    raise Stop(f"a property value of type {type(value).__name__}: {value!r:.80}")


def merge_types(key: str, seen: set[str]) -> str:
    """One column type for every value seen under `key`."""
    if len(seen) == 1:
        return next(iter(seen))
    for narrow, wide in (("long", "double"), ("long[]", "double[]")):
        if seen == {narrow, wide}:
            return wide
    raise Stop(f"property {key} holds values of different types: {sorted(seen)}")


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (DateTime, Date)):
        return value.iso_format()
    if isinstance(value, float):
        return repr(value)
    return str(value)


def csv_field(value) -> str:
    """One CSV field. Missing is empty; text is always quoted, so an empty
    string stays an empty string."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [_scalar(v) for v in value]
        if any(ARRAY_DELIMITER in part for part in parts):
            raise Stop(f"an array element contains {ARRAY_DELIMITER!r}: {value!r:.80}")
        value = ARRAY_DELIMITER.join(parts)
    elif not isinstance(value, str):
        return _scalar(value)
    return '"' + value.replace('"', '""') + '"'


def check_node(labels: list[str], keys: list[str]) -> None:
    unknown_labels = set(labels) - KEPT_LABELS
    if unknown_labels:
        raise Stop(f"label not on the keep list: {sorted(unknown_labels)}")
    if KEEP_ALL_KEYS_ON & set(labels):
        return
    unknown = set(keys) - KEPT_USER_KEYS
    if unknown:
        raise Stop(
            f"property not on the keep list (labels {labels}): {sorted(unknown)[:10]}"
        )


@dataclass
class Table:
    """Rows of one CSV file, with the column types met on the way."""

    path: Path
    fixed: list[str]
    types: dict[str, set[str]] = field(default_factory=dict)
    rows: int = 0
    empty_arrays: int = 0

    def __post_init__(self):
        # Rows are kept as JSON lines until every column is known, then written as CSV.
        self._raw = self.path.with_suffix(".jsonl").open("w")

    def add(self, fixed: list, props: dict) -> None:
        kept = {}
        for key, value in props.items():
            if value is None:
                continue
            if isinstance(value, list) and not value:
                self.empty_arrays += 1
                continue
            self.types.setdefault(key, set()).add(csv_type(value))
            kept[key] = value
        self._raw.write(json.dumps([fixed, kept], default=_json) + "\n")
        self.rows += 1

    def finish(self, header: list[str]) -> list[str]:
        """Write the header file and the data file; returns the property columns."""
        self._raw.close()
        columns = sorted(self.types)
        typed = [f"{key}:{merge_types(key, self.types[key])}" for key in columns]
        self.path.with_name(self.path.stem + "-header.csv").write_text(
            ",".join(header + typed) + "\n"
        )
        raw_path = self.path.with_suffix(".jsonl")
        with raw_path.open() as raw, self.path.open("w") as out:
            for line in raw:
                fixed, props = json.loads(line, object_hook=_unjson)
                fields = [csv_field(v) for v in fixed]
                fields += [csv_field(props.get(key)) for key in columns]
                out.write(",".join(fields) + "\n")
        raw_path.unlink()
        return columns


def _json(value):
    if isinstance(value, (DateTime, Date)):
        return {"__temporal__": type(value).__name__, "iso": value.iso_format()}
    raise TypeError(type(value).__name__)


def _unjson(obj: dict):
    if "__temporal__" in obj:
        kind = DateTime if obj["__temporal__"] == "DateTime" else Date
        return kind.from_iso_format(obj["iso"])
    return obj


async def _read(session, cypher: str, **params) -> list[dict]:
    async def work(tx):
        result = await tx.run(cypher, **params)
        return [dict(record) async for record in result]

    return await session.execute_read(work)


_USERS_WITH_KEY = """
MATCH (n:NostrUser) WHERE n[$key] IS NOT NULL RETURN count(n) AS users
"""


def unlisted_keys(keys: list[str]) -> list[str]:
    """Property keys that are neither scores nor on the user keep list."""
    return sorted(k for k in keys if not is_dropped(k) and k not in KEPT_USER_KEYS)


def unknown_in(found: dict) -> list[str]:
    """What an inventory holds that the export would stop on: labels and
    relationship types off the keep lists, and unlisted keys a user node carries."""
    unknown = [n for n, v in found["labels"].items() if v["action"] == "unknown"]
    unknown += [
        t for t, v in found["relationship_types"].items() if v["action"] == "unknown"
    ]
    unknown += [
        f"{key} (on {v['user_nodes']} user nodes)"
        for key, v in found["property_keys_unlisted"].items()
        if v["action"] == "unknown"
    ]
    return unknown


async def inventory(session) -> dict:
    """Labels, relationship types and property keys, each marked kept or unknown.
    Labels and types are counted from the graph: one nothing carries any more is
    not listed. The database never forgets a property key, so an unlisted one is
    unknown only if a user node carries it."""
    counts = await _counts(session)
    labels: Counter[str] = Counter()
    for label_set, n in counts["nodes_by_labels"].items():
        for label in label_set.split("+"):
            labels[label] += n
    keys = [
        r["propertyKey"]
        for r in await _read(
            session, "CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey"
        )
    ]

    def mark(name: str, kept: set[str]) -> str:
        return "kept" if name in kept else "unknown"

    unlisted = {}
    for key in unlisted_keys(keys):
        users = (await _read(session, _USERS_WITH_KEY, key=key))[0]["users"]
        unlisted[key] = {
            "user_nodes": users,
            "action": "unknown" if users else "not on a user node",
        }

    return {
        "labels": {
            label: {"nodes": n, "action": mark(label, KEPT_LABELS)}
            for label, n in sorted(labels.items())
        },
        "relationship_types": {
            t: {"relationships": n, "action": mark(t, KEPT_RELATIONSHIPS)}
            for t, n in sorted(counts["relationships_by_type"].items())
        },
        "property_keys_dropped": sum(1 for k in keys if is_dropped(k)),
        "property_keys_kept": sorted(k for k in keys if k in KEPT_USER_KEYS),
        "property_keys_unlisted": unlisted,
    }


async def _counts(session) -> dict:
    labels = await _read(session, "MATCH (n) RETURN labels(n) AS labels, count(*) AS n")
    rels = await _read(
        session, "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n"
    )
    return {
        "nodes_by_labels": {"+".join(sorted(r["labels"])): r["n"] for r in labels},
        "relationships_by_type": {r["type"]: r["n"] for r in rels},
    }


def carried_schema(rows: list[dict]) -> tuple[dict[str, str], list[str]]:
    """Constraints or indexes to recreate, by name, and the names left behind:
    those on a label the rebuild does not keep, or on a score property."""
    kept, left = {}, []
    allowed = KEPT_LABELS | KEPT_RELATIONSHIPS
    for row in rows:
        on = set(row["labelsOrTypes"] or [])
        if on <= allowed and not any(is_dropped(k) for k in row["properties"] or []):
            kept[row["name"]] = row["createStatement"]
        else:
            left.append(row["name"])
    return kept, sorted(left)


async def _schema(session) -> dict:
    constraints, left_constraints = carried_schema(
        await _read(
            session,
            "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, createStatement"
            " RETURN name, labelsOrTypes, properties, createStatement",
        ),
    )
    indexes, left_indexes = carried_schema(
        await _read(
            session,
            "SHOW INDEXES YIELD name, type, owningConstraint, labelsOrTypes, properties,"
            " createStatement WHERE owningConstraint IS NULL AND type <> 'LOOKUP'"
            " RETURN name, labelsOrTypes, properties, createStatement",
        ),
    )
    return {
        "constraints": constraints,
        "indexes": indexes,
        "left_behind": left_constraints + left_indexes,
    }


async def export(session, out: Path, page: int, log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    unknown = unknown_in(await inventory(session))
    if unknown:
        raise Stop(f"not on the keep lists: {unknown}")

    before = await _counts(session)
    top = (await _read(session, "MATCH (n) RETURN coalesce(max(id(n)), -1) AS top"))[0][
        "top"
    ]
    nodes = Table(out / "nodes.csv", [":ID", ":LABEL"])
    rels = Table(out / "relationships.csv", [":START_ID", ":END_ID", ":TYPE"])
    dropped_values = 0
    key_counts: Counter[str] = Counter()
    rel_key_counts: Counter[str] = Counter()
    samples: dict[str, dict] = {}
    other_nodes: list[dict] = []
    started = time.monotonic()
    for lo in range(0, top + 1, page):
        for row in await _read(session, _NODES, lo=lo, hi=lo + page):
            props = {k: v for k, v in row["kept"]}
            check_node(row["labels"], list(props))
            nodes.add([row["id"], ARRAY_DELIMITER.join(sorted(row["labels"]))], props)
            dropped_values += row["dropped"]
            if "NostrUser" in row["labels"]:
                key_counts.update(
                    k for k, v in props.items() if v is not None and v != []
                )
                if (
                    len(samples) < SAMPLE_SIZE
                    and row["id"] % max(top // SAMPLE_SIZE, 1) == 0
                ):
                    samples[props["pubkey"]] = {"properties": props}
            if "NostrUser" not in row["labels"]:
                # Few (graph-streamer's state node): compared whole after the import.
                other_nodes.append(
                    {"labels": sorted(row["labels"]), "properties": props}
                )
        for row in await _read(session, _RELATIONSHIPS, lo=lo, hi=lo + page):
            rels.add([row["start"], row["end"], row["type"]], row["props"])
            rel_key_counts.update(
                f"{row['type']}.{k}"
                for k, v in row["props"].items()
                if v is not None and v != []
            )
        done = min(lo + page, top + 1)
        rate = done / max(time.monotonic() - started, 1e-9)
        log(f"  node ids up to {done} of {top + 1}, {rate:.0f}/s")

    for row in await _read(
        session, _DEGREES, pubkeys=list(samples), types=sorted(KEPT_RELATIONSHIPS)
    ):
        samples[row["pubkey"]]["out"] = row["out_degrees"]
        samples[row["pubkey"]]["in"] = row["in_degrees"]

    nodes.finish([":ID", ":LABEL"])
    rels.finish([":START_ID", ":END_ID", ":TYPE"])
    after = await _counts(session)
    if after != before or sum(before["nodes_by_labels"].values()) != nodes.rows:
        raise Stop(
            "the graph changed during the export (or rows were missed): stop every"
            f" writer and export again. before {before}, after {after}, exported"
            f" {nodes.rows} nodes and {rels.rows} relationships"
        )
    if sum(before["relationships_by_type"].values()) != rels.rows:
        raise Stop(f"exported {rels.rows} relationships, the database has {before}")

    summary = {
        **before,
        "user_property_counts": dict(sorted(key_counts.items())),
        "relationship_property_counts": dict(sorted(rel_key_counts.items())),
        "other_nodes": sorted(
            other_nodes, key=lambda n: json.dumps(n, default=_json, sort_keys=True)
        ),
        "dropped_values": dropped_values,
        "empty_arrays_not_carried": nodes.empty_arrays + rels.empty_arrays,
        "schema": await _schema(session),
        "samples": samples,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=_json))
    (out / "import-command.txt").write_text(
        "neo4j-admin database import full neo4j --overwrite-destination"
        f' --id-type=integer --array-delimiter="{ARRAY_DELIMITER}" --multiline-fields=true'
        " --nodes=/import/nodes-header.csv,/import/nodes.csv"
        " --relationships=/import/relationships-header.csv,/import/relationships.csv\n"
    )
    return summary


async def apply_schema(session, summary: dict, apply: bool, log=print) -> list[str]:
    """Constraints and indexes the source had and this database lacks."""
    have = await _schema(session)
    missing = []
    for kind in ("constraints", "indexes"):
        for name, statement in summary["schema"][kind].items():
            if name in have[kind]:
                continue
            missing.append(name)
            if apply:
                await (await session.run(statement)).consume()
                log(f"  created {name}")
    if apply and missing:
        await (await session.run("CALL db.awaitIndexes(3600)")).consume()
    return missing


def _plain(value):
    """Values as they compare after a JSON round trip."""
    return json.loads(json.dumps(value, default=_json))


async def check(session, summary: dict) -> list[str]:
    """Everything the rebuilt database must agree with the export on."""
    problems = []
    now = await _counts(session)
    for part in ("nodes_by_labels", "relationships_by_type"):
        if now[part] != summary[part]:
            problems.append(f"{part}: exported {summary[part]}, rebuilt {now[part]}")

    keys = [
        r["k"]
        for r in await _read(
            session, "CALL db.propertyKeys() YIELD propertyKey AS k RETURN k"
        )
    ]
    leftover = [k for k in keys if is_dropped(k)]
    if leftover:
        problems.append(f"{len(leftover)} score property keys survived: {leftover[:5]}")

    wanted = summary["user_property_counts"]
    counted = await _read(
        session,
        "MATCH (n:NostrUser) UNWIND keys(n) AS k RETURN k, count(*) AS n",
    )
    got = {r["k"]: r["n"] for r in counted}
    if got != wanted:
        problems.append(f"user property counts: exported {wanted}, rebuilt {got}")

    counted = await _read(
        session,
        "MATCH ()-[r]->() UNWIND keys(r) AS k RETURN type(r) + '.' + k AS k, count(*) AS n",
    )
    got = {r["k"]: r["n"] for r in counted}
    if got != summary["relationship_property_counts"]:
        problems.append(
            f"relationship property counts: exported {summary['relationship_property_counts']},"
            f" rebuilt {got}"
        )

    others = await _read(
        session,
        "MATCH (n) WHERE NOT n:NostrUser RETURN labels(n) AS labels, properties(n) AS props",
    )
    rebuilt_others = sorted(
        (
            {"labels": sorted(r["labels"]), "properties": _plain(r["props"])}
            for r in others
        ),
        key=lambda n: json.dumps(n, sort_keys=True),
    )
    if rebuilt_others != _plain(summary["other_nodes"]):
        problems.append(
            f"nodes that are not users: exported {summary['other_nodes']}, rebuilt {rebuilt_others}"
        )

    samples = summary["samples"]
    rows = await _read(
        session,
        "MATCH (n:NostrUser) WHERE n.pubkey IN $pubkeys RETURN n.pubkey AS pubkey,"
        " properties(n) AS props",
        pubkeys=list(samples),
    )
    rebuilt = {r["pubkey"]: {"properties": _plain(r["props"])} for r in rows}
    for row in await _read(
        session, _DEGREES, pubkeys=list(samples), types=sorted(KEPT_RELATIONSHIPS)
    ):
        rebuilt[row["pubkey"]]["out"] = row["out_degrees"]
        rebuilt[row["pubkey"]]["in"] = row["in_degrees"]
    for pubkey, sample in samples.items():
        expected = dict(
            sample,
            properties={
                k: v
                for k, v in sample["properties"].items()
                if v is not None and v != []
            },
        )
        if rebuilt.get(pubkey) != expected:
            problems.append(
                f"sampled node {pubkey}: exported {expected}, rebuilt {rebuilt.get(pubkey)}"
            )

    missing = await apply_schema(session, summary, apply=False)
    if missing:
        problems.append(f"constraints or indexes not recreated: {missing}")
    states = await _read(
        session,
        "SHOW INDEXES YIELD name, state WHERE state <> 'ONLINE' RETURN name, state",
    )
    if states:
        problems.append(f"indexes not online: {states}")
    return problems


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory")
    exp = sub.add_parser("export")
    exp.add_argument("--out", required=True, type=Path)
    exp.add_argument("--page", type=int, default=5000, help="node ids per read")
    sch = sub.add_parser("schema")
    sch.add_argument("--dir", required=True, type=Path)
    sch.add_argument("--apply", action="store_true")
    chk = sub.add_parser("check")
    chk.add_argument("--dir", required=True, type=Path)
    args = ap.parse_args()

    started = time.monotonic()
    try:
        # id() is deprecated in Neo4j 5 and warned about on every page read.
        async with neo4j_driver.session(
            notifications_disabled_classifications=["DEPRECATION"]
        ) as session:
            if args.command == "inventory":
                found = await inventory(session)
                print(json.dumps(found, indent=1))
                unknown = unknown_in(found)
                if unknown:
                    raise Stop(f"not on the keep lists: {unknown}")
                return 0
            if args.command == "export":
                summary = await export(session, args.out, args.page)
                print(f"nodes: {summary['nodes_by_labels']}")
                print(f"relationships: {summary['relationships_by_type']}")
                print(f"score values left behind: {summary['dropped_values']}")
                print(f"schema left behind: {summary['schema']['left_behind']}")
                print(
                    f"empty arrays not carried: {summary['empty_arrays_not_carried']}"
                )
                print(f"written to {args.out} in {time.monotonic() - started:.1f}s")
                print(f"next: {(args.out / 'import-command.txt').read_text().strip()}")
                return 0
            summary = json.loads((args.dir / "summary.json").read_text())
            if args.command == "schema":
                missing = await apply_schema(session, summary, args.apply)
                print(
                    f"missing: {missing}" if not args.apply else f"created: {missing}"
                )
                return 0
            problems = await check(session, summary)
            for problem in problems:
                print(f"  {problem[:600]}")
            print(f"problems: {len(problems)}")
            return 1 if problems else 0
    except Stop as stop:
        print(f"stopped: {stop}")
        return 2
    finally:
        await neo4j_driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
