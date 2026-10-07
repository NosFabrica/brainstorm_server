"""The clean-up script strips leftover `trusted_reporters_<observer>` props in batches.

Scoped to synthetic observer keys so real local data is never written::

    poetry run pytest tests/integration/test_clean_trusted_reporters_props_integration.py -m integration
"""

import asyncio

import pytest

from scripts.clean_trusted_reporters_props import (
    count_props,
    discover_keys,
    remove_props,
)
from tests.integration.preset_graph import fresh_driver

pytestmark = pytest.mark.integration

_PREFIX = "ltr06test_"
_KEY_A = f"trusted_reporters_{_PREFIX}obs_a"
_KEY_B = f"trusted_reporters_{_PREFIX}obs_b"
_KEEP = {
    f"influence_{_PREFIX}obs_a": 0.5,
    f"hops_{_PREFIX}obs_a": 2,
    f"trusted_followers_{_PREFIX}obs_a": 7,
}
# pubkey suffix -> leftover keys planted on it
_NODES = {
    "both": [_KEY_A, _KEY_B],
    "a1": [_KEY_A],
    "a2": [_KEY_A],
    "b1": [_KEY_B],
    "a3": [_KEY_A],
    "none": [],
}


async def _run(fn):
    driver = fresh_driver()
    try:
        async with driver.session() as session:
            return await fn(session)
    finally:
        await driver.close()


async def _seed(session):
    for name, keys in _NODES.items():
        props = {**_KEEP, **{k: 3 for k in keys}}
        await session.run(
            "MERGE (u:NostrUser {pubkey: $pk}) SET u += $props",
            pk=_PREFIX + name,
            props=props,
        )


async def _teardown(session):
    await session.run(
        "MATCH (u:NostrUser) WHERE u.pubkey STARTS WITH $p DETACH DELETE u",
        p=_PREFIX,
    )


async def _props_of_synthetic(session):
    res = await session.run(
        "MATCH (u:NostrUser) WHERE u.pubkey STARTS WITH $p "
        "RETURN u.pubkey AS pk, properties(u) AS props",
        p=_PREFIX,
    )
    return {r["pk"]: r["props"] async for r in res}


@pytest.fixture
def seeded():
    asyncio.run(_run(_seed))
    try:
        yield
    finally:
        asyncio.run(_run(_teardown))


# Node-id range per transaction; large so the shared local graph is few chunks.
_ID_CHUNK = 200_000


def test_discovers_observer_keys_from_the_database(seeded):
    keys = asyncio.run(_run(discover_keys))
    assert {_KEY_A, _KEY_B} <= set(keys)
    assert all(k.startswith("trusted_reporters_") for k in keys)


def test_counts_props_and_nodes_for_the_given_keys(seeded):
    counts = asyncio.run(_run(lambda s: count_props(s, [_KEY_A, _KEY_B])))
    assert counts == (6, 5)


def test_apply_removes_in_batches_and_rerun_finds_none(seeded):
    keys = [_KEY_A, _KEY_B]
    first = asyncio.run(_run(lambda s: remove_props(s, keys, batch=_ID_CHUNK)))
    assert first.nodes == 5
    assert asyncio.run(_run(lambda s: count_props(s, keys))) == (0, 0)

    second = asyncio.run(_run(lambda s: remove_props(s, keys, batch=_ID_CHUNK)))
    assert (second.nodes, second.batches) == (0, 0)


def test_apply_leaves_other_properties_untouched(seeded):
    asyncio.run(_run(lambda s: remove_props(s, [_KEY_A, _KEY_B], batch=_ID_CHUNK)))
    props = asyncio.run(_run(_props_of_synthetic))
    assert len(props) == len(_NODES)
    for pk, p in props.items():
        assert p == {"pubkey": pk, **_KEEP}
