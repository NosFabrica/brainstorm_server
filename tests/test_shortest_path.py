"""Fast-suite tests for GET /shortestPath (story: shortest-path #1, ADR 0001).

These cover the criteria that need NO graph backend: input validation (AC6)
and the from == to short-circuit (AC4, ADR 0001 Decision 1 — answered before
any Neo4j session is opened, so no mocking is required).

Graph-dependent criteria (AC1/AC2/AC3/AC5 and npub equivalence on real data)
live in tests/integration/test_shortest_path_integration.py.
"""

import pytest
from nostr_sdk import Keys


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
    assert data["path"] == [pk]
    assert data["pathCount"] == 1
    assert data["pathCountCapped"] is False
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
        {"maxPaths": 0},
        {"maxPaths": 1001},
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
