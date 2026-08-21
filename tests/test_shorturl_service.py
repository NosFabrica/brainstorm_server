"""Minting and resolving short links — the storage-facing half of the shortener.

The repo layer is mocked so these run in the fast suite. Input validation is not
here — it lives in the request schema, covered by ``test_shorturl_contract.py``.
The pure helpers are in ``test_shorturl_codes.py``, and a real-Postgres round
trip in ``tests/integration/test_shorturl_roundtrip.py``.

Issue: .scratch/shorturl/issues/02-durable-storage.md
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.services import shorturl_service as svc

PK = "f4d1866e8599563c52ceeedf11c28b8567e465c6e9a91df92add535d57f02ab0"
RELAYS = ["wss://relay.damus.io", "wss://nos.lol"]


def _row(short_code="MBD5M41Y", pubkey=PK, relays=None):
    return SimpleNamespace(
        short_code=short_code,
        pubkey=pubkey,
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


@pytest.fixture
def repo(monkeypatch):
    """Patch the three repo functions the service calls."""
    fns = SimpleNamespace(
        by_content=AsyncMock(return_value=None),
        by_code=AsyncMock(return_value=None),
        insert=AsyncMock(),
    )
    monkeypatch.setattr(svc, "select_short_url_by_content_on_db", fns.by_content)
    monkeypatch.setattr(svc, "select_short_url_by_code_on_db", fns.by_code)
    monkeypatch.setattr(svc, "insert_short_url_on_db", fns.insert)
    return fns


# --------------------------------------------------------------------------
# minting
# --------------------------------------------------------------------------


def test_a_first_mint_inserts_and_returns_the_new_code(repo):
    repo.insert.return_value = _row("AB3XK9QZ")

    code, content = _run(svc.create_short_url(_FakeSession(), PK, RELAYS))

    assert code == "AB3XK9QZ"
    assert content.pubkey == PK
    assert content.relays == RELAYS
    assert repo.insert.await_count == 1


def test_the_same_pubkey_and_relay_set_returns_the_existing_code(repo):
    """AC: 'the same pubkey and relay set returns the same code'."""
    repo.by_content.return_value = _row("MBD5M41Y")

    code, _ = _run(svc.create_short_url(_FakeSession(), PK, RELAYS))

    assert code == "MBD5M41Y"
    assert repo.insert.await_count == 0, "a dedup hit must not insert"


def test_an_equivalent_relay_set_hits_the_same_fingerprint(repo):
    """Reordered / re-cased / trailing-slash sets must look up the same row."""
    repo.by_content.return_value = _row("MBD5M41Y")

    _run(
        svc.create_short_url(
            _FakeSession(), PK, ["wss://NOS.lol/", "wss://relay.damus.io"]
        )
    )

    _, _, fingerprint = repo.by_content.await_args.args
    assert fingerprint == svc.relays_fingerprint(RELAYS)


def test_a_dedup_hit_returns_the_stored_relays_not_the_callers(repo):
    """Deliberate: the POST response must agree with what a later GET resolves."""
    stored = ["wss://relay.damus.io", "wss://nos.lol"]
    repo.by_content.return_value = _row("MBD5M41Y", relays=stored)

    _, content = _run(
        svc.create_short_url(
            _FakeSession(), PK, ["wss://NOS.lol/", "wss://relay.damus.io"]
        )
    )

    assert content.relays == stored


def test_an_empty_relay_list_mints_its_own_code(repo):
    """`[]` is a legitimate content value with its own fingerprint."""
    repo.insert.return_value = _row("7YJR9PD6", relays=[])

    code, content = _run(svc.create_short_url(_FakeSession(), PK, []))

    assert code == "7YJR9PD6"
    assert content.relays == []


def test_a_code_collision_is_retried(repo):
    """A unique violation on the code retries with a fresh one."""
    repo.insert.side_effect = [
        IntegrityError("insert", {}, Exception("duplicate short_code")),
        _row("SH4R8CKY"),
    ]
    # No concurrent row exists, so the retry path is taken rather than the
    # settle-on-the-winner path.
    repo.by_content.side_effect = [None, None]

    code, _ = _run(svc.create_short_url(_FakeSession(), PK, RELAYS))

    assert code == "SH4R8CKY"
    assert repo.insert.await_count == 2


def test_a_concurrent_mint_settles_on_the_winner(repo):
    """Losing the (pubkey, relay-set) race returns the winner's code, not a retry."""
    winner = _row("7YJR9PD6")
    repo.insert.side_effect = IntegrityError("insert", {}, Exception("uq_ violation"))
    repo.by_content.side_effect = [None, winner]

    code, content = _run(svc.create_short_url(_FakeSession(), PK, RELAYS))

    assert code == "7YJR9PD6"
    assert content.relays == winner.relays
    assert repo.insert.await_count == 1, "the loser must not keep retrying"


def test_giving_up_after_repeated_collisions_is_a_500(repo):
    repo.insert.side_effect = IntegrityError("insert", {}, Exception("dup"))
    repo.by_content.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        _run(svc.create_short_url(_FakeSession(), PK, RELAYS))
    assert excinfo.value.status_code == 500


# --------------------------------------------------------------------------
# resolving
# --------------------------------------------------------------------------


def test_an_unknown_code_is_a_404(repo):
    """AC: 'Unknown code returns 404'."""
    repo.by_code.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        _run(svc.get_short_url_content(_FakeSession(), "ZZZZZZZZ"))
    assert excinfo.value.status_code == 404


def test_resolving_returns_the_stored_content(repo):
    repo.by_code.return_value = _row("MBD5M41Y")

    content = _run(svc.get_short_url_content(_FakeSession(), "MBD5M41Y"))

    assert content.pubkey == PK
    assert content.relays == RELAYS


@pytest.mark.parametrize(
    "typed", ["MBD5M41Y", "mbd5m41y", "MbD5m41Y", "MBD5M4IY", " mbd5m41y "]
)
def test_the_lookup_is_normalized_before_it_reaches_the_repo(repo, typed):
    """AC: lowercase, uppercase, mixed case, and I/L/O folding all resolve."""
    repo.by_code.return_value = _row("MBD5M41Y")

    _run(svc.get_short_url_content(_FakeSession(), typed))

    _, looked_up = repo.by_code.await_args.args
    assert looked_up == "MBD5M41Y"
