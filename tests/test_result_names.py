import asyncio

import pytest

from app.message_queue_tasks import result_names

OBSERVER = "a" * 64
ALICE = "b" * 64
BOB = "c" * 64
USERS = {1: OBSERVER, 2: ALICE, 3: BOB}


@pytest.fixture(autouse=True)
def users(monkeypatch):
    asked: list[list[int]] = []

    async def pubkeys_for(ids):
        asked.append(list(ids))
        return {i: USERS[i] for i in ids if i in USERS}

    monkeypatch.setattr(result_names, "pubkeys_for", pubkeys_for)
    return asked


def _by_id():
    return {
        "observer": OBSERVER,
        "scorecards": None,
        "scorecardsById": {
            "1": {"hops": 0, "influence": 1.0},
            "2": {"hops": 1, "influence": 0.5, "trusted_followers": 3},
        },
        "changedScoreIds": [2],
        "droppedBelowCutoffIds": [],
        "neo4jWriteIds": [2],
        "neo4jClearIds": [3],
        "changedScorePubkeys": None,
        "neo4jWritePubkeys": None,
        "neo4jClearPubkeys": None,
        "rounds": 7,
        "success": True,
    }


def test_an_id_named_result_becomes_the_pubkey_form(users):
    result = _by_id()
    asyncio.run(result_names.name_by_pubkey(result))
    assert result == {
        "scorecards": {
            OBSERVER: {
                "hops": 0,
                "influence": 1.0,
                "observer": OBSERVER,
                "observee": OBSERVER,
            },
            ALICE: {
                "hops": 1,
                "influence": 0.5,
                "trusted_followers": 3,
                "observer": OBSERVER,
                "observee": ALICE,
            },
        },
        "changedScorePubkeys": [ALICE],
        "droppedBelowCutoffPubkeys": [],
        "neo4jWritePubkeys": [ALICE],
        "neo4jClearPubkeys": [BOB],
        "rounds": 7,
        "success": True,
    }
    # One lookup for the whole result, the cleared user included.
    assert users == [[1, 2, 3]]


def test_a_write_list_that_was_not_computed_still_means_write_everything():
    result = _by_id()
    result["neo4jWriteIds"] = None
    asyncio.run(result_names.name_by_pubkey(result))
    assert result["neo4jWritePubkeys"] is None


def test_a_pubkey_named_result_is_left_alone(users):
    result = {"scorecards": {ALICE: {"observee": ALICE}}, "success": True}
    before = {"scorecards": {ALICE: {"observee": ALICE}}, "success": True}
    asyncio.run(result_names.name_by_pubkey(result))
    assert result == before and users == []
    failure = {"scorecards": None, "success": False}
    asyncio.run(result_names.name_by_pubkey(failure))
    assert failure == {"scorecards": None, "success": False}


def test_an_id_without_a_pubkey_fails_the_run():
    result = _by_id()
    result["scorecardsById"]["99"] = {"hops": 2}
    with pytest.raises(RuntimeError, match="1 user ids in the result have no pubkey"):
        asyncio.run(result_names.name_by_pubkey(result))


def test_the_lookup_is_batched(monkeypatch, users):
    monkeypatch.setattr(result_names, "LOOKUP_BATCH", 2)
    asyncio.run(result_names.name_by_pubkey(_by_id()))
    assert users == [[1, 2], [3]]
