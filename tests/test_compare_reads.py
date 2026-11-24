"""compare_reads: normalise, hash, record, in-process reads, diff, choose Observers."""
import asyncio
import json

import pytest

from scripts import compare_reads as cr

HOUSE, OBS = "h" * 64, "a" * 64


def _hash(body, status=200):
    return cr.digest(status, json.dumps(body).encode())[0]


def test_timestamps_and_request_ids_do_not_change_the_hash():
    a = {"data": {"influence": 0.5, "updated_at": "2026-11-23T00:00:00Z"}}
    b = {"data": {"influence": 0.5, "updated_at": "2026-11-24T00:00:00Z"}}
    assert _hash(a) == _hash(b)
    assert _hash({"requestId": 1, "x": 1}) == _hash({"requestId": 2, "x": 1})


def test_tie_order_is_stable_but_real_order_is_kept():
    tied_a = {
        "items": [{"pubkey": "b", "influence": 0.5}, {"pubkey": "a", "influence": 0.5}]
    }
    tied_b = {
        "items": [{"pubkey": "a", "influence": 0.5}, {"pubkey": "b", "influence": 0.5}]
    }
    assert _hash(tied_a) == _hash(tied_b)
    ordered = {
        "items": [{"pubkey": "a", "influence": 0.9}, {"pubkey": "b", "influence": 0.5}]
    }
    flipped = {
        "items": [{"pubkey": "b", "influence": 0.5}, {"pubkey": "a", "influence": 0.9}]
    }
    assert _hash(ordered) != _hash(flipped)


def test_set_like_lists_are_unordered_and_index_lists_are_not():
    assert _hash({"pubkeys": ["a", "b"]}) == _hash({"pubkeys": ["b", "a"]})
    assert _hash({"links": [[0], [1]]}) != _hash({"links": [[1], [0]]})


def test_any_value_status_or_missing_key_changes_the_hash():
    assert _hash({"influence": 0.5}) != _hash({"influence": 0.5000000001})
    assert _hash({"x": 1}, 200) != _hash({"x": 1}, 500)
    assert _hash({"x": 1, "y": None}) != _hash({"x": 1})


def test_item_count():
    assert cr.item_count({"data": {"items": [1, 2], "next_cursor": None}}) == 2
    assert cr.item_count({"data": {"pubkeys": [1]}}) == 1
    assert cr.item_count({"data": {"influence": 1}}) is None


def _world(influence=0.5):
    """A fake server: two pages of following, everything else a small body."""

    async def fetch(observer, path, params):
        if path == "connections":
            if params.get("cursor") == "p2":
                items, cursor = [{"pubkey": "c" * 64, "influence": 0.1}], None
            else:
                items, cursor = [{"pubkey": "b" * 64, "influence": influence}], "p2"
            body = {"data": {"items": items, "next_cursor": cursor}}
        else:
            body = {"data": {"path": path, "params": params, "generated_at": "now"}}
        return 200, json.dumps(body).encode()

    return fetch


def test_record_follows_cursors_and_writes_one_entry_per_call(tmp_path):
    entries = asyncio.run(
        cr.record(_world(), [OBS], tmp_path, raw=True, log=lambda _: None)
    )
    pages = [e for e in entries if e.endpoint == "connections"]
    first_pages = len(cr._connections_calls(OBS))
    assert len(pages) == 2 * first_pages
    assert any(
        e.endpoint == "overview" and ["pubkey", "b" * 64] in e.params for e in entries
    )
    assert any(e.endpoint == "shortest_path" for e in entries)
    assert (tmp_path / "observers.txt").read_text() == f"{OBS}\n"
    assert len(cr.load(tmp_path)) == len(entries)
    assert len(list((tmp_path / "raw").iterdir())) == len(entries)


def test_diff_of_identical_records_is_empty_and_a_change_is_found(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    quiet = dict(log=lambda _: None)
    asyncio.run(cr.record(_world(), [OBS], a, raw=True, **quiet))
    asyncio.run(cr.record(_world(), [OBS], b, raw=False, **quiet))
    asyncio.run(cr.record(_world(0.6), [OBS], c, raw=False, **quiet))
    assert cr.differences(cr.load(a), cr.load(b)) == []

    found = cr.differences(cr.load(a), cr.load(c))
    assert found and all(d.key.split("|")[0] == "connections" for d in found)
    where = asyncio.run(cr.save_mismatches(found, a, c, _world(0.6)))
    names = {p.name for p in where.iterdir()}
    f = found[0].a.call.file
    assert {f"{f}.a.json", f"{f}.now.json", f"{f}.call.json"} <= names
    note = json.loads((where / f"{f}.call.json").read_text())
    assert note["now"] == found[0].b.hash != note["a"]


def test_a_call_only_in_one_record_is_a_difference(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    asyncio.run(cr.record(_world(), [OBS], a, raw=False, log=lambda _: None))
    asyncio.run(cr.record(_world(), [OBS, HOUSE], b, raw=False, log=lambda _: None))
    found = cr.differences(cr.load(a), cr.load(b))
    assert found and all(d.a is None and d.b.observer == HOUSE for d in found)


def test_a_whitelist_change_is_not_a_difference_but_a_failing_one_is():
    def entry(endpoint, digest, status=200):
        e = cr.Entry(endpoint, OBS, [], status, digest, 1)
        return {e.call.key: e}

    assert cr.differences(entry("whitelist", "x"), entry("whitelist", "y")) == []
    assert cr.differences(entry("whitelist", "x"), entry("whitelist", "x", 500))
    assert cr.differences(entry("whitelist", "x"), {})
    assert cr.differences(entry("overview", "x"), entry("overview", "y"))


def test_an_http_error_from_a_service_is_recorded_as_its_status():
    from fastapi import HTTPException

    async def read(endpoint, observer, params):
        raise HTTPException(status_code=404, detail="no such user")

    status, raw = asyncio.run(cr.service_fetcher(read)(OBS, "overview", {}))
    assert (status, json.loads(raw)) == (404, {"detail": "no such user"})


class _Cutoffs:
    verified_line, reporter = 0.2, 0.1


@pytest.fixture
def services(monkeypatch):
    """Every service the reader calls, recording the Observer it was given."""
    from contextlib import asynccontextmanager

    from app.core import database
    from app.schemas import schemas
    from app.services import (
        graph_service,
        network_alerts_service,
        user_service,
        verified_cutoffs,
    )

    seen = {}

    @asynccontextmanager
    async def no_db():
        yield object()

    async def cutoffs(db, observer):
        seen["cutoffs"] = observer
        return _Cutoffs()

    async def connections(**kw):
        seen["connections"] = kw
        return schemas.PaginatedUserConnections(
            items=[schemas.UserConnectionItem(pubkey="b" * 64, influence=0.5)],
            next_cursor="c1",
        )

    async def updated_at(db, observer):
        seen["whitelist"] = observer
        return "stamp"

    async def whitelist(db, observer):
        return ["d" * 64, "c" * 64]

    monkeypatch.setattr(database, "db_session", no_db)
    monkeypatch.setattr(verified_cutoffs, "resolve_verified_cutoffs", cutoffs)
    monkeypatch.setattr(user_service, "get_user_connections", connections)
    monkeypatch.setattr(user_service, "whitelist_updated_at", updated_at)
    monkeypatch.setattr(user_service, "get_whitelisted_pubkeys_of_observer", whitelist)
    monkeypatch.setattr(graph_service, "get_shortest_follow_path", None)
    monkeypatch.setattr(network_alerts_service, "get_network_alerts_for_observer", None)
    return seen


def test_connections_are_read_as_the_observer_and_serialised_like_the_api(services):
    fetch = cr.service_fetcher()
    params = {
        "pubkey": OBS,
        "kind": "followed_by",
        "limit": "50",
        "cursor": "c0",
        "tier": "high",
        "verified_only": "true",
    }
    status, raw = asyncio.run(fetch(HOUSE, "connections", params))
    assert status == 200
    body = json.loads(raw)
    assert body["data"]["items"][0] == {
        "pubkey": "b" * 64,
        "influence": 0.5,
        "trusted_reporters": None,
        "tier": None,
    }
    assert body["data"]["next_cursor"] == "c1" and body["code"] == 200
    kw = services["connections"]
    assert services["cutoffs"] == HOUSE and kw["observer"] == HOUSE
    assert (kw["pubkey"], kw["limit"], kw["cursor"], kw["order"]) == (
        OBS,
        50,
        "c0",
        "desc",
    )
    assert kw["verified_only"] is True and kw["with_total"] is False


def test_the_whitelist_is_the_observers_and_empty_without_a_stamp(
    services, monkeypatch
):
    from app.services import user_service

    fetch = cr.service_fetcher()
    data = json.loads(asyncio.run(fetch(OBS, "whitelist", {}))[1])["data"]
    assert services["whitelist"] == OBS
    assert data == {
        "observerPubkey": OBS,
        "numPubkeys": 2,
        "pubkeys": ["d" * 64, "c" * 64],
    }

    async def none(db, observer):
        return None

    monkeypatch.setattr(user_service, "whitelist_updated_at", none)
    data = json.loads(asyncio.run(fetch(OBS, "whitelist", {}))[1])["data"]
    assert data["numPubkeys"] == 0


def test_an_unknown_endpoint_is_refused(services):
    with pytest.raises(ValueError):
        asyncio.run(cr.service_fetcher()(OBS, "nope", {}))


def test_choose_observers_covers_house_largest_newest_free_and_paid():
    sizes = {f"{i:064x}": i * 10 for i in range(1, 41)}
    big = sorted(sizes, key=lambda o: -sizes[o])
    newest = [f"{99:064x}", f"{5:064x}"]  # the newest has no scores yet
    free = [f"{i:064x}" for i in range(1, 30)]
    paid = [f"{i:064x}" for i in range(30, 41)]
    chosen = cr.choose_observers(HOUSE, sizes, newest, free, paid, n=20)
    assert len(chosen) == len(set(chosen)) == 20
    assert chosen[0] == HOUSE
    assert chosen[1:4] == big[:3]
    assert chosen[4] == f"{5:064x}"
    assert len([o for o in chosen if o in paid]) >= 2
    assert f"{99:064x}" not in chosen
    assert cr.choose_observers(HOUSE, sizes, newest, free, paid, n=20) == chosen
