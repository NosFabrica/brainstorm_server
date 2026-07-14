"""Integration tests for GET /shortestPath against the real local Neo4j.

Requires the local stack (Neo4j reachable at ``settings.neo4j_db_url`` — on
this machine ``bolt://localhost:7688`` via ``.env``). Run explicitly with::

    poetry run pytest tests/integration/test_shortest_path_integration.py -m integration

Seeds a small synthetic ``NostrUser``/``FOLLOWS`` fixture with freshly
generated keys (no collision with organic graph data) and removes it on
teardown:

    alice -> b1 -> carol -> dave -> alice     (dave->alice closes a cycle)
    alice -> b2 -> carol                       (second 2-hop path to carol)
    alice -> loner                             (loner has NO outgoing edges)

Known distances (directed):
    alice -> carol : 2 hops, exactly 2 shortest paths (via b1 / via b2)
    alice -> dave  : 3 hops, 2 shortest paths
    loner -> alice : unreachable (reverse edge alice->loner exists)

Story: engineering-team/stories/shortest-path/1-get-shortest-path.md
ADR:   engineering-team/decisions/shortest-path/0001-shortest-path-query-and-placement.md
"""

import asyncio

import pytest
from neo4j import AsyncGraphDatabase
from nostr_sdk import Keys

from app.core.config import settings

pytestmark = pytest.mark.integration

N_RANDOM_CALLS = 40  # AC2: P(all-same | uniform over 2 paths) = 2^-39


@pytest.fixture(scope="module")
def graph():
    """Seed the fixture graph; yield {name: hex_pubkey}; clean up after."""
    names = ["alice", "b1", "b2", "carol", "dave", "loner"]
    pks = {name: Keys.generate().public_key().to_hex() for name in names}
    # ghost is intentionally NEVER seeded into Neo4j.
    pks["ghost"] = Keys.generate().public_key().to_hex()

    edges = [
        ("alice", "b1"),
        ("alice", "b2"),
        ("b1", "carol"),
        ("b2", "carol"),
        ("carol", "dave"),
        ("dave", "alice"),
        ("alice", "loner"),
    ]

    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        try:
            async with driver.session() as session:
                await session.run(
                    "UNWIND $pubkeys AS pk MERGE (:NostrUser {pubkey: pk})",
                    pubkeys=[pks[n] for n in names],
                )
                for src, dst in edges:
                    await session.run(
                        "MATCH (a:NostrUser {pubkey: $src}), (b:NostrUser {pubkey: $dst}) "
                        "MERGE (a)-[:FOLLOWS]->(b)",
                        src=pks[src],
                        dst=pks[dst],
                    )
        finally:
            await driver.close()

    async def _teardown() -> None:
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_db_url,
            auth=(settings.neo4j_db_username, settings.neo4j_db_password),
        )
        try:
            async with driver.session() as session:
                await session.run(
                    "MATCH (u:NostrUser) WHERE u.pubkey IN $pubkeys DETACH DELETE u",
                    pubkeys=list(pks.values()),
                )
        finally:
            await driver.close()

    asyncio.run(_seed())
    try:
        yield pks
    finally:
        asyncio.run(_teardown())


def _get(client, params: dict):
    return client.get("/shortestPath", params=params)


def _shortest_alice_carol_chains(pks) -> set[tuple[str, ...]]:
    return {
        (pks["alice"], pks["b1"], pks["carol"]),
        (pks["alice"], pks["b2"], pks["carol"]),
    }


# ---------------------------------------------------------------------------
# AC1 — reachable pair, full response shape
# ---------------------------------------------------------------------------
def test_reachable_pair_full_shape(client, graph):
    resp = _get(client, {"from": graph["alice"], "to": graph["carol"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["reachable"] is True
    assert data["hops"] == 2
    assert tuple(data["path"]) in _shortest_alice_carol_chains(graph)
    assert data["pathCount"] == 2
    assert data["pathCountCapped"] is False
    assert data["from"] == graph["alice"]
    assert data["to"] == graph["carol"]
    assert data["maxHops"] == 30


# ---------------------------------------------------------------------------
# AC6 (mix) + AC1 (echo) — npub inputs behave exactly like hex
# ---------------------------------------------------------------------------
def test_npub_and_hex_inputs_are_equivalent(client, graph):
    from nostr_sdk import PublicKey

    alice_npub = PublicKey.parse(graph["alice"]).to_bech32()

    resp = _get(client, {"from": alice_npub, "to": graph["carol"]})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is True
    assert data["hops"] == 2
    # Echo is canonical hex, not the npub as given.
    assert data["from"] == graph["alice"]
    assert data["to"] == graph["carol"]


# ---------------------------------------------------------------------------
# AC2 — returned path is a random member of the shortest-path set
# ---------------------------------------------------------------------------
def test_returned_path_is_random_member_of_shortest_set(client, graph):
    valid = _shortest_alice_carol_chains(graph)
    seen: set[tuple[str, ...]] = set()

    for _ in range(N_RANDOM_CALLS):
        resp = _get(client, {"from": graph["alice"], "to": graph["carol"]})
        assert resp.status_code == 200
        chain = tuple(resp.json()["data"]["path"])
        assert chain in valid  # membership on every call
        seen.add(chain)

    # Random selection: with 2 shortest paths and 40 uniform draws the odds of
    # never seeing the second one are 2^-39 — if this fires, selection is not
    # random.
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# AC3 — unreachable variants
# ---------------------------------------------------------------------------
def test_reverse_only_edge_is_unreachable(client, graph):
    resp = _get(client, {"from": graph["loner"], "to": graph["alice"]})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is False
    assert data["hops"] is None
    assert data["path"] is None
    assert data["pathCount"] == 0
    assert data["pathCountCapped"] is False


def test_unknown_pubkey_is_unreachable(client, graph):
    resp = _get(client, {"from": graph["alice"], "to": graph["ghost"]})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is False
    assert data["hops"] is None
    assert data["path"] is None
    assert data["pathCount"] == 0


def test_maxhops_gates_reachability(client, graph):
    # True distance alice -> dave is 3.
    too_low = _get(
        client, {"from": graph["alice"], "to": graph["dave"], "maxHops": 2}
    )
    assert too_low.status_code == 200
    assert too_low.json()["data"]["reachable"] is False
    assert too_low.json()["data"]["maxHops"] == 2

    exact = _get(
        client, {"from": graph["alice"], "to": graph["dave"], "maxHops": 3}
    )
    assert exact.status_code == 200
    data = exact.json()["data"]
    assert data["reachable"] is True
    assert data["hops"] == 3
    assert data["pathCount"] == 2


# ---------------------------------------------------------------------------
# AC5 — pathCount cap
# ---------------------------------------------------------------------------
def test_pathcount_is_capped_at_maxpaths(client, graph):
    resp = _get(
        client, {"from": graph["alice"], "to": graph["carol"], "maxPaths": 1}
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reachable"] is True
    assert data["hops"] == 2
    assert data["pathCount"] == 1
    assert data["pathCountCapped"] is True
    assert tuple(data["path"]) in _shortest_alice_carol_chains(graph)
