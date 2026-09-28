"""GET /whitelisted/{observer_pubkey}/ranks — the downloadable observee → Rank list.

Another app pulls a whole observer's network (~300k keys) and filters it by
Rank itself, so the shape is bucketed by Rank and the list is public, like
/whitelisted.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.database import get_db

OBSERVER = "a" * 64
UPDATED_AT = datetime(2026, 9, 28, 12, 0, 0)
PATH = f"/whitelisted/{OBSERVER}/ranks"


class _Scalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _WhitelistSession:
    """Answers the updated_at meta-read and the rank query (rows already ordered
    Rank DESC, key — as the SQL orders them)."""

    def __init__(self, *, updated_at=UPDATED_AT, rows=None) -> None:
        self.updated_at = updated_at
        self.rows = rows or []
        self.params: list = []

    async def execute(self, stmt, params=None):
        if params is None:
            return SimpleNamespace(scalars=lambda: _Scalars(self.updated_at))
        self.params.append(params)
        return iter(self.rows)


@pytest.fixture
def session():
    return _WhitelistSession(
        rows=[("c" * 64, 57), ("b" * 64, 3), ("d" * 64, 3), ("e" * 64, 2)]
    )


def _client(client, session):
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    return client


def test_ranks_come_bucketed_highest_first(client, session):
    response = _client(client, session).get(PATH)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["observerPubkey"] == OBSERVER
    assert data["numPubkeys"] == 4
    assert data["ranks"] == {
        "57": ["c" * 64],
        "3": ["b" * 64, "d" * 64],
        "2": ["e" * 64],
    }
    assert list(data["ranks"]) == ["57", "3", "2"]


def test_min_rank_defaults_to_the_cutoff_and_is_passed_through(client, session):
    c = _client(client, session)

    c.get(PATH)
    c.get(PATH, params={"minRank": 40})

    assert [p["min_rank"] for p in session.params] == [2, 40]


@pytest.mark.parametrize("min_rank", [0, 1, 101])
def test_min_rank_outside_the_stored_range_is_rejected(client, session, min_rank):
    response = _client(client, session).get(PATH, params={"minRank": min_rank})

    assert response.status_code == 422


def test_unknown_observer_is_an_empty_list(client):
    session = _WhitelistSession(updated_at=None)

    response = _client(client, session).get(PATH)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "observerPubkey": OBSERVER,
        "numPubkeys": 0,
        "ranks": {},
    }
    assert session.params == []


def test_unchanged_snapshot_answers_304_without_the_rank_query(client, session):
    c = _client(client, session)
    etag = c.get(PATH).headers["etag"]
    session.params.clear()

    response = c.get(PATH, headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert session.params == []


def test_etag_varies_with_min_rank_and_differs_from_whitelisted(client, session):
    c = _client(client, session)

    default = c.get(PATH).headers["etag"]
    filtered = c.get(PATH, params={"minRank": 40}).headers["etag"]
    pubkeys_only = c.get(f"/whitelisted/{OBSERVER}").headers["etag"]

    assert len({default, filtered, pubkeys_only}) == 3


def test_ranks_are_public(session):
    # No verify_token override: an anonymous consumer app can download it.
    from fastapi.testclient import TestClient

    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    try:
        assert TestClient(app).get(PATH).status_code == 200
    finally:
        app.dependency_overrides.clear()
