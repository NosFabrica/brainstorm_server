"""The score copy: which properties are scores, and that it never writes Neo4j."""
import asyncio
import inspect
import math
import re

from scripts import copy_scores_to_postgres as mod

OBS = "a" * 64
OTHER = "b" * 64


def test_only_the_three_score_fields_of_well_formed_observers_are_read():
    props = {
        "pubkey": "c" * 64,
        "user_id": 7,
        "nip05_status": "VERIFIED",
        f"influence_{OBS}": 0.5,
        f"hops_{OBS}": 2,
        f"trusted_followers_{OBS}": 9,
        f"hops_{OTHER}": 3,
        f"trusted_reporters_{OBS}": 4,  # no longer a score
        f"influence_{'A' * 64}": 0.1,  # not a lowercase hex pubkey
        "influence_short": 0.2,
    }
    assert mod.scores_on_node(props) == {OBS: [0.5, 2, 9], OTHER: [None, 3, None]}


def test_a_page_casts_counts_and_drops_non_finite_influence():
    nodes = [
        {
            "pubkey": "c" * 64,
            "props": {
                "user_id": 7,
                f"influence_{OBS}": math.nan,
                # A pre-cutover worker stored counts as floats.
                f"hops_{OBS}": 2.0,
                f"trusted_followers_{OBS}": 9.0,
            },
        },
        {"pubkey": "d" * 64, "props": {f"influence_{OBS}": 0.5}},  # no user_id
        {"pubkey": "e" * 64, "props": {"user_id": 8}},  # no scores
    ]
    page = asyncio.run(mod.build_page(nodes, {OBS: 1}))
    assert page.rows == [(1, 7, None, 2, 9)]
    assert (page.rows_without_user_id, page.non_finite) == (1, 1)


def test_only_keeps_one_observer():
    nodes = [
        {
            "pubkey": "c" * 64,
            "props": {
                "user_id": 7,
                f"influence_{OBS}": 0.5,
                f"influence_{OTHER}": 0.25,
            },
        }
    ]
    page = asyncio.run(mod.build_page(nodes, {OBS: 1, OTHER: 2}, only=OTHER))
    assert page.rows == [(2, 7, 0.25, None, None)]


_WRITES = re.compile(r"\b(SET|CREATE|MERGE|DELETE|REMOVE|DROP)\b")


def test_no_cypher_in_the_script_writes():
    """Every Cypher string in the module, checked for a write clause."""
    source = inspect.getsource(mod)
    cypher = [mod._PAGE, mod._PAGE_OF, mod._PAGE_OF_BY_ID, mod._SCORED_WITHOUT_ID]
    cypher += re.findall(r'session\.run\(\s*"((?:[^"\\]|\\.)*)"', source)
    cypher += re.findall(r'"(CALL db\.[^"]*)"', source)
    assert len(cypher) >= 3
    for statement in cypher:
        assert not _WRITES.search(statement), statement


def test_node_pages_are_read_in_a_read_transaction():
    calls = []

    class _Session:
        async def execute_read(self, work):
            calls.append("read")
            return []

        async def execute_write(self, work):  # pragma: no cover
            raise AssertionError("the copy wrote to Neo4j")

    assert asyncio.run(mod._read_page(_Session(), "", 10)) == []
    assert calls == ["read"]


def test_no_cypher_variable_is_a_float_literal():
    for statement in (
        mod._PAGE,
        mod._PAGE_OF,
        mod._PAGE_OF_BY_ID,
        mod._SCORED_WITHOUT_ID,
    ):
        assert not re.search(r"\bAS\s+(inf|infinity|nan)\b", statement, re.I)


async def _aiter(items):
    for item in items:
        yield item


def _compare(expected, stored):
    check = mod.RowCheck()
    names = {1: OBS, 2: OTHER}
    asyncio.run(
        mod.compare_rows(
            _aiter(expected),
            {o: _aiter(rows) for o, rows in stored.items()},
            names,
            check,
        )
    )
    return check


def test_identical_rows_verify():
    check = _compare(
        [(7, {1: (0.5, 2, 9), 2: (None, 3, None)}), (9, {1: (0.25, 1, 0)})],
        {1: [(7, 0.5, 2, 9), (9, 0.25, 1, 0)], 2: [(7, None, 3, None)]},
    )
    assert check.ok and check.rows == 3


def test_every_kind_of_row_difference_is_named_by_observer():
    check = _compare(
        [(7, {1: (0.5, 2, 9)}), (9, {1: (0.25, 1, 0)}), (11, {2: (0.1, 2, 1)})],
        {
            # 7 differs in the last bit, 9 is missing, 10 and 12 are extra.
            1: [(7, 0.5 + 1e-16 * 2, 2, 9), (10, 0.3, 1, 0), (12, 0.3, 1, 0)],
            2: [(11, 0.1, 2, 2)],
        },
    )
    assert check.bad == {OBS: 4, OTHER: 1}
    assert any("missing" in line for line in check.samples[OBS])
    assert sum("only in scorecard" in line for line in check.samples[OBS]) == 2


def test_an_observer_without_stored_rows_misses_all_of_them():
    check = _compare([(7, {1: (0.5, 2, 9)}), (8, {1: (0.5, 2, 9)})], {})
    assert check.bad == {OBS: 2}


def test_scores_on_nodes_without_a_user_id_fail_verify():
    check = mod.RowCheck(rows_without_user_id=1)
    assert not check.ok


def test_verify_reads_nodes_in_a_read_transaction_by_user_id():
    seen = []

    class _Result:
        def __init__(self, records):
            self.records = records

        def __aiter__(self):
            return _aiter(self.records).__aiter__()

    class _Tx:
        async def run(self, cypher, **params):
            seen.append(params["after"])
            if params["after"] == -1:
                return _Result([{"user_id": 7, "found": [[f"influence_{OBS}", 0.5]]}])
            return _Result([])

    class _Session:
        async def execute_read(self, work):
            return await work(_Tx())

        async def execute_write(self, work):  # pragma: no cover
            raise AssertionError("verify wrote to Neo4j")

    async def collect():
        return [r async for r in mod._expected_rows(_Session(), [OBS], {OBS: 1}, 10)]

    assert asyncio.run(collect()) == [(7, {1: (0.5, None, None)})]
    assert seen == [-1, 7]
