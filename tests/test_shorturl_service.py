"""Minting and resolving short links — the storage-facing half of the shortener.

The repo layer is mocked so these run in the fast suite. Input validation is not
here — it lives in the request schema, covered by ``test_shorturl_contract.py``.
Keys and paths are in ``test_shorturl_entities.py``, code format in
``test_shorturl_codes.py``, and a real-Postgres round trip in
``tests/integration/test_shorturl_roundtrip.py``.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.services import shorturl_service as svc

PK = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"
NPUB = "npub17ngcvm59n9trc5kwam03rs5ts4n7gewxax53m7f2m4f464ls92cqr5qjta"
NPROFILE = "nprofile1qqs0f5vxd6zej43u2t8wahc3c29c2elyvhrwn2galy4d656a2lcz4vqpp4mhxue69uhkummn9ekx7mqhx9ed4"
RELAYS = ["wss://nos.lol"]


def _row(short_code="MBD5M41Y", kind="profile", key=PK, relays=None):
    return SimpleNamespace(
        short_code=short_code,
        kind=kind,
        entity_key=key,
        relays=RELAYS if relays is None else relays,
    )


class _FakeSession:
    """Just enough session for the savepoint the service opens."""

    def begin_nested(self):
        class _Savepoint:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Savepoint()


def _run(coro):
    return asyncio.run(coro)


def _mint(kind="profile", key=PK, relays=RELAYS):
    return _run(svc.create_short_url(_FakeSession(), kind, key, relays))


@pytest.fixture
def repo(monkeypatch):
    """Patch the three repo functions the service calls."""
    fns = SimpleNamespace(
        by_entity=AsyncMock(return_value=None),
        by_code=AsyncMock(return_value=None),
        insert=AsyncMock(),
    )
    monkeypatch.setattr(svc, "select_short_url_by_entity_on_db", fns.by_entity)
    monkeypatch.setattr(svc, "select_short_url_by_code_on_db", fns.by_code)
    monkeypatch.setattr(svc, "insert_short_url_on_db", fns.insert)
    return fns


# --------------------------------------------------------------------------
# minting
# --------------------------------------------------------------------------


def test_a_first_mint_stores_the_entity_and_its_relays(repo):
    repo.insert.return_value = _row("AB3XK9QZ")

    created = _mint()

    assert created.short_code == "AB3XK9QZ"
    assert (created.kind, created.key, created.relays) == ("profile", PK, RELAYS)
    assert created.path == f"/p/{NPROFILE}"
    _, kwargs = repo.insert.await_args
    assert (kwargs["kind"], kwargs["entity_key"], kwargs["relays"]) == (
        "profile",
        PK,
        RELAYS,
    )


def test_the_same_entity_returns_the_existing_code(repo):
    repo.by_entity.return_value = _row("MBD5M41Y")

    created = _mint()

    assert created.short_code == "MBD5M41Y"
    assert repo.by_entity.await_args.args[1:] == ("profile", PK)
    assert repo.insert.await_count == 0, "a dedup hit must not insert"


def test_a_later_mint_never_changes_the_stored_relays(repo):
    """First wins: the response agrees with what a later GET resolves."""
    repo.by_entity.return_value = _row("MBD5M41Y", relays=[])

    created = _mint(relays=["wss://other.example"])

    assert created.relays == []
    assert created.path == f"/p/{NPUB}"
    assert repo.insert.await_count == 0


def test_a_code_collision_is_retried(repo):
    repo.insert.side_effect = [
        IntegrityError("insert", {}, Exception("duplicate short_code")),
        _row("SH4R8CKY"),
    ]
    repo.by_entity.side_effect = [None, None]

    created = _mint()

    assert created.short_code == "SH4R8CKY"
    assert repo.insert.await_count == 2


def test_a_concurrent_mint_settles_on_the_winner(repo):
    """Losing the (kind, key) race returns the winner's code and relays, not a retry."""
    winner = _row("7YJR9PD6", relays=[])
    repo.insert.side_effect = IntegrityError("insert", {}, Exception("uq_ violation"))
    repo.by_entity.side_effect = [None, winner]

    created = _mint()

    assert created.short_code == "7YJR9PD6"
    assert created.relays == []
    assert repo.insert.await_count == 1, "the loser must not keep retrying"


def test_giving_up_after_repeated_collisions_is_a_500(repo):
    repo.insert.side_effect = IntegrityError("insert", {}, Exception("dup"))
    repo.by_entity.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        _mint()
    assert excinfo.value.status_code == 500


def test_a_dlist_address_mint_does_not_store_dcosl(repo):
    key = f"39999:{PK}:x"
    repo.insert.return_value = _row(kind="address", key=key, relays=[])

    created = _mint(kind="address", key=key, relays=[])

    assert repo.insert.await_args.kwargs["relays"] == []
    assert created.relays == []
    assert created.path.startswith("/e/naddr1")


# --------------------------------------------------------------------------
# resolving
# --------------------------------------------------------------------------


def test_an_unknown_code_is_a_404(repo):
    repo.by_code.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        _run(svc.get_short_url_entity(_FakeSession(), "ZZZZZZZZ"))
    assert excinfo.value.status_code == 404


def test_resolving_returns_the_entity_and_its_full_path(repo):
    repo.by_code.return_value = _row("MBD5M41Y", kind="hashtag", key="nostr", relays=[])

    entity = _run(svc.get_short_url_entity(_FakeSession(), "MBD5M41Y"))

    assert (entity.kind, entity.key, entity.relays, entity.path) == (
        "hashtag",
        "nostr",
        [],
        "/t/nostr",
    )


@pytest.mark.parametrize(
    "typed", ["MBD5M41Y", "mbd5m41y", "MbD5m41Y", "MBD5M4IY", " mbd5m41y "]
)
def test_the_lookup_is_normalized_before_it_reaches_the_repo(repo, typed):
    repo.by_code.return_value = _row("MBD5M41Y")

    _run(svc.get_short_url_entity(_FakeSession(), typed))

    _, looked_up = repo.by_code.await_args.args
    assert looked_up == "MBD5M41Y"
