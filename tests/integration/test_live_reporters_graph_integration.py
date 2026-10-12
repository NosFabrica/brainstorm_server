"""The legacy full-graph reads count verified reporters live, like the profile reads.

GET /user/{pubkey} and the deprecated GET /user/self put the live count of
reporters strictly above the observer's preset reporter cutoff on every row::

    poetry run pytest tests/integration/test_live_reporters_graph_integration.py -m integration
"""

import asyncio
from datetime import datetime

import pytest
from fastapi import Request

import app.routers.user.router as user_router_module
from app.api import app
from app.schemas.schemas import UserHistoryInstance
from app.utils.api_validators import verify_token
from app.utils.auth.auth_models import JWTData
from app.utils.observer import default_observer_pubkey
from tests.integration.preset_graph import (
    DEFAULT_CUTOFFS,
    PERMISSIVE_CUTOFFS,
    RESTRICTIVE_CUTOFFS,
    api,
    fresh_driver,
    seed_graph,
)
from tests.test_verified_cutoffs import saved_presets  # noqa: F401

pytestmark = pytest.mark.integration

# Reporter cutoffs: PERMISSIVE 0.002, DEFAULT 0.1, RESTRICTIVE 0.5.
_NODES: dict[str, tuple[float | None, int]] = {
    "subject": (0.4, 0),
    "rep_hi_1": (0.9, 0),
    "rep_hi_2": (0.9, 0),
    "rep_at_default": (0.1, 0),
    "rep_mid": (0.3, 0),
    "two_above": (0.005, 0),
    "one_at_cutoff": (0.005, 0),
    "swing": (0.001, 0),
}

_EDGES: list[tuple[str, str, str]] = [
    ("two_above", "FOLLOWS", "subject"),
    ("one_at_cutoff", "FOLLOWS", "subject"),
    ("swing", "FOLLOWS", "subject"),
    ("subject", "FOLLOWS", "swing"),
    ("rep_hi_1", "REPORTS", "two_above"),
    ("rep_hi_2", "REPORTS", "two_above"),
    ("rep_hi_1", "REPORTS", "one_at_cutoff"),
    ("rep_at_default", "REPORTS", "one_at_cutoff"),
    ("rep_hi_1", "REPORTS", "swing"),
    ("rep_mid", "REPORTS", "swing"),
]

# Expected live counts per preset for (two_above, one_at_cutoff, swing).
_LIVE = {
    "permissive": (2, 2, 2),
    "default": (2, 1, 2),
    "restrictive": (2, 1, 1),
}


async def _set_props_as_observer(graph: dict[str, str], observer: str) -> None:
    """Mirror each node's default-observer influence under `observer`, and plant
    a stale stored reporter count the reads must ignore."""
    default_key = f"influence_{default_observer_pubkey()}"
    driver = fresh_driver()
    try:
        async with driver.session() as session:
            await session.run(
                "MATCH (u:NostrUser) WHERE u.pubkey IN $pks "
                f"SET u.`influence_{observer}` = u.`{default_key}`, "
                f"u.`trusted_reporters_{observer}` = 99",
                pks=list(graph.values()),
            )
    finally:
        await driver.close()


@pytest.fixture(scope="module")
def graph():
    for pks in seed_graph(_NODES, _EDGES):
        asyncio.run(_set_props_as_observer(pks, default_observer_pubkey()))
        asyncio.run(_set_props_as_observer(pks, pks["subject"]))
        yield pks


def _counts(graph, data: dict) -> tuple[int, int, int]:
    rows = {r["pubkey"]: r for r in data["followed_by"]}
    return tuple(
        rows[graph[n]]["trusted_reporters"]
        for n in ("two_above", "one_at_cutoff", "swing")
    )


async def _user_graph(client, pubkey: str) -> dict:
    resp = await client.get(f"/user/{pubkey}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@pytest.mark.parametrize(
    "cutoffs, preset",
    [
        (PERMISSIVE_CUTOFFS, "permissive"),
        (DEFAULT_CUTOFFS, "default"),
        (RESTRICTIVE_CUTOFFS, "restrictive"),
    ],
)
def test_user_graph_rows_carry_the_live_count_under_the_preset(graph, cutoffs, preset):
    async def body():
        async with api(cutoffs) as client:
            data = await _user_graph(client, graph["subject"])
        assert _counts(graph, data) == _LIVE[preset]
        following = {r["pubkey"]: r for r in data["following"]}
        assert following[graph["swing"]]["trusted_reporters"] == _LIVE[preset][2]
        assert set(data) == {
            "influence",
            "followed_by",
            "following",
            "muted_by",
            "muting",
            "reported_by",
            "reporting",
        }
        assert set(data["followed_by"][0]) == {
            "pubkey",
            "influence",
            "trusted_reporters",
        }

    asyncio.run(body())


@pytest.fixture
def signed_in_as_subject(graph, monkeypatch):
    async def _fake_verify_token(request: Request) -> None:
        request.state.jwt_data = JWTData(
            nostr_pubkey=graph["subject"], expires_date=datetime.max
        )

    async def _no_history(_db, pubkey):
        now = datetime.now()
        return UserHistoryInstance(
            pubkey=pubkey,
            ta_pubkey=pubkey,
            last_time_calculated_graperank=None,
            last_time_triggered_graperank=None,
            created_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(user_router_module, "get_user_history_data", _no_history)
    app.dependency_overrides[verify_token] = _fake_verify_token
    yield
    app.dependency_overrides.pop(verify_token, None)


async def _self_graph(client) -> dict:
    resp = await client.get("/user/self")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["graph"]


def test_self_rows_carry_the_live_count_under_the_callers_own_preset(
    graph, signed_in_as_subject, saved_presets
):
    saved_presets[graph["subject"]] = "RESTRICTIVE"

    async def body():
        # The viewer-preset override must not leak in: /self is the caller's.
        async with api(DEFAULT_CUTOFFS) as client:
            data = await _self_graph(client)
        assert _counts(graph, data) == _LIVE["restrictive"]

    asyncio.run(body())


def test_an_unreadable_preset_serves_default(graph, signed_in_as_subject, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("graperank_preset row missing")

    monkeypatch.setattr(
        "app.services.verified_cutoffs.get_graperank_preset_by_pubkey_on_db", _boom
    )

    async def body():
        async with api(None) as client:
            by_pubkey = await _user_graph(client, graph["subject"])
            own = await _self_graph(client)
        assert _counts(graph, by_pubkey) == _LIVE["default"]
        assert _counts(graph, own) == _LIVE["default"]

    asyncio.run(body())
