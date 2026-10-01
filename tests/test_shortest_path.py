"""Fast-suite tests for GET /shortestPath (ADR 0004).

Validation and the from == to short-circuit need no graph backend. The Path
network cases fake the shortest-paths query at the repo boundary.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j.exceptions import ClientError, Neo4jError
from nostr_sdk import Keys

from app.repos.user_repo import ShortestPathTimeout, get_all_shortest_follow_paths
from tests.conftest import count_walks


def _get(client, params: dict):
    return client.get("/shortestPath", params=params)


# ---------------------------------------------------------------------------
# AC4 — self-path short-circuit
# ---------------------------------------------------------------------------
def test_self_path_returns_zero_hops(client):
    pk = Keys.generate().public_key().to_hex()

    resp = _get(client, {"from": pk, "to": pk})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is True
    assert data["hops"] == 0
    assert data["pathCount"] == 1
    assert data["layers"] == []
    assert data["links"] == []
    assert data["from"] == pk
    assert data["to"] == pk
    assert data["maxHops"] == 30


def test_self_path_accepts_mixed_hex_and_npub(client):
    keys = Keys.generate()
    hex_pk = keys.public_key().to_hex()
    npub = keys.public_key().to_bech32()

    resp = _get(client, {"from": hex_pk, "to": npub})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["hops"] == 0
    # Echo is canonical hex regardless of input form (AC1/AC6).
    assert data["from"] == hex_pk
    assert data["to"] == hex_pk


# ---------------------------------------------------------------------------
# AC6 — input validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "not-a-pubkey",
        "nprofile1qqstest",  # other NIP-19 forms are rejected
        "8f3fbb5129dcb9194c02b67c1b41a3f60dd369663f53499b2b3c72a73c6fa9",  # 62 chars
        "npub1invalidinvalidinvalid",
    ],
)
def test_invalid_from_pubkey_is_400(client, bad):
    ok = Keys.generate().public_key().to_hex()

    resp = _get(client, {"from": bad, "to": ok})

    assert resp.status_code == 400


def test_invalid_to_pubkey_is_400(client):
    ok = Keys.generate().public_key().to_hex()

    resp = _get(client, {"from": ok, "to": "garbage"})

    assert resp.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"maxHops": 0},
        {"maxHops": 51},
    ],
)
def test_out_of_bounds_params_are_422(client, overrides):
    pk_a = Keys.generate().public_key().to_hex()
    pk_b = Keys.generate().public_key().to_hex()

    resp = _get(client, {"from": pk_a, "to": pk_b, **overrides})

    assert resp.status_code == 422


def test_missing_required_param_is_422(client):
    pk = Keys.generate().public_key().to_hex()

    resp = _get(client, {"from": pk})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Path network — graph answers faked at the repo boundary
# ---------------------------------------------------------------------------
@pytest.fixture
def paths_repo(monkeypatch):
    """Fakes the shortest-paths query; set `.return_value` to the chains it finds."""
    repo = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.services.graph_service.get_all_shortest_follow_paths", repo
    )

    @asynccontextmanager
    async def _fake_session():
        yield AsyncMock()

    fake_driver = MagicMock()
    fake_driver.session = lambda: _fake_session()
    monkeypatch.setattr("app.services.graph_service.neo4j_driver", fake_driver)
    return repo


def _pk() -> str:
    return Keys.generate().public_key().to_hex()


def test_two_hops_is_one_sorted_layer(client, paths_repo):
    a, z = _pk(), _pk()
    m1, m2, m3 = sorted([_pk(), _pk(), _pk()])
    paths_repo.return_value = [[a, m3, z], [a, m1, z], [a, m2, z]]

    resp = _get(client, {"from": a, "to": z})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is True
    assert data["hops"] == 2
    assert data["pathCount"] == 3
    assert data["layers"] == [[m1, m2, m3]]
    assert data["links"] == []


def test_three_hops_links_index_into_the_sorted_next_layer(client, paths_repo):
    # The PRD's worked example: A/B both reach D and E, C reaches only E.
    you, vitor = _pk(), _pk()
    a, b, c = sorted([_pk(), _pk(), _pk()])
    d, e = sorted([_pk(), _pk()])
    paths_repo.return_value = [
        [you, b, e, vitor],
        [you, a, d, vitor],
        [you, c, e, vitor],
        [you, a, e, vitor],
        [you, b, d, vitor],
    ]

    data = _get(client, {"from": you, "to": vitor}).json()["data"]

    assert data["hops"] == 3
    assert data["pathCount"] == 5
    assert data["layers"] == [[a, b, c], [d, e]]
    assert data["links"] == [[[0, 1], [0, 1], [1]]]


def test_four_hops_walks_through_the_network_equal_path_count(client, paths_repo):
    you, z = _pk(), _pk()
    a, b = sorted([_pk(), _pk()])
    c = _pk()
    d, e = sorted([_pk(), _pk()])
    paths_repo.return_value = [
        [you, a, c, d, z],
        [you, a, c, e, z],
        [you, b, c, d, z],
        [you, b, c, e, z],
    ]

    data = _get(client, {"from": you, "to": z}).json()["data"]

    assert data["layers"] == [[a, b], [c], [d, e]]
    assert data["links"] == [[[0], [0]], [[0, 1]]]
    assert data["pathCount"] == 4
    assert count_walks(data) == 4


def test_one_hop_has_no_connectors(client, paths_repo):
    a, z = _pk(), _pk()
    paths_repo.return_value = [[a, z]]

    data = _get(client, {"from": a, "to": z}).json()["data"]

    assert data["reachable"] is True
    assert data["hops"] == 1
    assert data["pathCount"] == 1
    assert data["layers"] == []
    assert data["links"] == []


def test_unreachable_is_an_empty_network(client, paths_repo):
    paths_repo.return_value = []

    data = _get(client, {"from": _pk(), "to": _pk()}).json()["data"]

    assert data["reachable"] is False
    assert data["hops"] is None
    assert data["pathCount"] == 0
    assert data["layers"] == []
    assert data["links"] == []


def test_same_paths_in_any_order_give_identical_bodies(client, paths_repo):
    you, z = _pk(), _pk()
    a, b, c, d = (_pk() for _ in range(4))
    paths = [[you, a, c, z], [you, b, c, z], [you, b, d, z], [you, a, d, z]]

    paths_repo.return_value = paths
    first = _get(client, {"from": you, "to": z}).content
    paths_repo.return_value = list(reversed(paths))
    second = _get(client, {"from": you, "to": z}).content

    assert first == second


def test_query_timeout_is_504_never_a_partial_network(client, paths_repo):
    paths_repo.side_effect = ShortestPathTimeout()

    resp = _get(client, {"from": _pk(), "to": _pk()})

    assert resp.status_code == 504
    assert isinstance(resp.json()["detail"], str)


def _neo4j_error(code: str):
    # Built the way the driver builds errors off the wire.
    return Neo4jError._hydrate_neo4j(code=code, message="x")


def test_repo_maps_neo4j_transaction_timeout_to_shortest_path_timeout():
    session = AsyncMock()
    session.run.side_effect = _neo4j_error(
        "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
    )

    with pytest.raises(ShortestPathTimeout):
        asyncio.run(get_all_shortest_follow_paths(session, _pk(), _pk(), 30))


def test_repo_lets_other_neo4j_client_errors_through():
    session = AsyncMock()
    session.run.side_effect = _neo4j_error("Neo.ClientError.Statement.SyntaxError")

    with pytest.raises(ClientError):
        asyncio.run(get_all_shortest_follow_paths(session, _pk(), _pk(), 30))
