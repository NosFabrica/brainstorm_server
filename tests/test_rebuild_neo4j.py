"""The Neo4j rebuild export: what is kept, how values become CSV, and what stops it."""
import inspect
import json
import re

import pytest
from neo4j.time import DateTime

from scripts import rebuild_neo4j as mod

OBS = "a" * 64


def test_score_properties_are_dropped_and_nothing_else():
    for key in (
        f"influence_{OBS}",
        f"hops_{OBS}",
        f"trusted_followers_{OBS}",
        f"trusted_reporters_{OBS}",
        "score_shard",
    ):
        assert mod.is_dropped(key), key
    for key in mod.KEPT_USER_KEYS:
        assert not mod.is_dropped(key), key


def test_a_property_or_label_nobody_listed_stops_the_export():
    mod.check_node(
        ["NostrUser"], ["pubkey", "user_id", "nip05_status", "report_events"]
    )
    mod.check_node(["GraphStreamerState"], ["anything", "goes"])
    with pytest.raises(mod.Stop, match="property not on the keep list"):
        mod.check_node(["NostrUser"], ["pubkey", "a_new_field"])
    with pytest.raises(mod.Stop, match="label not on the keep list"):
        mod.check_node(["NostrUser", "Surprise"], ["pubkey"])


def test_column_types():
    assert mod.csv_type(True) == "boolean"
    assert mod.csv_type(7) == "long"
    assert mod.csv_type(0.5) == "double"
    assert mod.csv_type("x") == "string"
    assert mod.csv_type(["a", "b"]) == "string[]"
    assert mod.csv_type([1, 2.5]) == "double[]"
    assert mod.csv_type(DateTime(2026, 1, 2, 3, 4, 5)) == "datetime"
    with pytest.raises(mod.Stop):
        mod.csv_type(["a", 1])
    with pytest.raises(mod.Stop):
        mod.csv_type({"a": 1})
    assert mod.merge_types("k", {"long", "double"}) == "double"
    with pytest.raises(mod.Stop, match="different types"):
        mod.merge_types("k", {"long", "string"})


def test_csv_fields():
    assert mod.csv_field(None) == ""
    assert mod.csv_field("") == '""'  # an empty string is not a missing one
    assert mod.csv_field('say "hi", ok') == '"say ""hi"", ok"'
    assert mod.csv_field("two\nlines") == '"two\nlines"'
    assert mod.csv_field(42) == "42"
    assert mod.csv_field(0.1) == "0.1"
    assert mod.csv_field(False) == "false"
    assert mod.csv_field(["1700000000:ab", "1700000001:cd"]) == (
        '"1700000000:ab;1700000001:cd"'
    )
    with pytest.raises(mod.Stop, match="array element contains"):
        mod.csv_field(["a;b"])


def test_a_table_writes_a_typed_header_and_sparse_rows(tmp_path):
    table = mod.Table(tmp_path / "nodes.csv", [":ID", ":LABEL"])
    table.add(
        [1, "NostrUser"], {"pubkey": "p1", "user_id": 5, "report_events": ["1:a"]}
    )
    table.add([2, "NostrUser"], {"pubkey": "p2", "nip05": "", "gone": None, "none": []})
    assert table.finish([":ID", ":LABEL"]) == [
        "nip05",
        "pubkey",
        "report_events",
        "user_id",
    ]
    assert (tmp_path / "nodes-header.csv").read_text() == (
        ":ID,:LABEL,nip05:string,pubkey:string,report_events:string[],user_id:long\n"
    )
    assert (tmp_path / "nodes.csv").read_text() == (
        '1,"NostrUser",,"p1","1:a",5\n' '2,"NostrUser","","p2",,\n'
    )
    assert (table.rows, table.empty_arrays) == (2, 1)
    assert not (tmp_path / "nodes.jsonl").exists()


def test_temporal_values_survive_the_intermediate_file(tmp_path):
    table = mod.Table(tmp_path / "nodes.csv", [":ID", ":LABEL"])
    table.add([1, "NostrUser"], {"seen": DateTime(2026, 1, 2, 3, 4, 5)})
    table.finish([":ID", ":LABEL"])
    assert "seen:datetime" in (tmp_path / "nodes-header.csv").read_text()
    assert "2026-01-02T03:04:05" in (tmp_path / "nodes.csv").read_text()
    assert json.loads(json.dumps({"v": DateTime(2026, 1, 2)}, default=mod._json))


def test_schema_on_labels_not_kept_or_on_score_properties_is_left_behind():
    rows = [
        {
            "name": "nostr_user_pubkey",
            "labelsOrTypes": ["NostrUser"],
            "properties": ["pubkey"],
            "createStatement": "A",
        },
        {
            "name": "nostr_user_score_shard",
            "labelsOrTypes": ["NostrUser"],
            "properties": ["score_shard"],
            "createStatement": "B",
        },
        {
            "name": "score_shard_unique",
            "labelsOrTypes": ["ScoreShard"],
            "properties": ["shard", "pubkey"],
            "createStatement": "C",
        },
        {
            "name": "bench_user_pubkey",
            "labelsOrTypes": ["BenchUser"],
            "properties": ["pubkey"],
            "createStatement": "D",
        },
        {
            "name": "streamer_follows_at",
            "labelsOrTypes": ["NostrUser"],
            "properties": ["follows_at"],
            "createStatement": "E",
        },
    ]
    kept, left = mod.carried_schema(rows)
    assert kept == {"nostr_user_pubkey": "A", "streamer_follows_at": "E"}
    assert left == ["bench_user_pubkey", "nostr_user_score_shard", "score_shard_unique"]


_WRITES = re.compile(r"\b(SET|CREATE|MERGE|DELETE|REMOVE|DROP)\b")


def test_the_export_reads_only():
    """Only `schema --apply` writes, and it runs statements read from the source."""
    for name in ("_NODES", "_RELATIONSHIPS", "_DEGREES"):
        assert not _WRITES.search(getattr(mod, name)), name
    for fn in (mod.inventory, mod._counts, mod._schema, mod.export, mod.check):
        for cypher in re.findall(
            r'"((?:MATCH|CALL|SHOW)[^"]*)"', inspect.getsource(fn)
        ):
            assert not _WRITES.search(cypher), cypher
    assert "execute_write" not in inspect.getsource(mod)


def test_no_cypher_alias_is_a_float_literal():
    assert not re.search(r"\bAS\s+(inf|infinity|nan)\b", inspect.getsource(mod), re.I)
