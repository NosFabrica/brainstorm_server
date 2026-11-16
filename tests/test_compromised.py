"""Compromised accounts: the pure parts of `compromised_service`, the published
baseline check, the zeroing selection and signing, and `/admin/compromised`.
The pipeline's hold is in test_publish_run.py (TAs), test_run_tier_counts.py
(whitelist) and the Neo4j writer's integration suite."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import Event, Keys

from app.message_queue_tasks.ta_signing import TA_KIND
from app.repos.brainstorm_nsec import _pack_pubkeys, holds_pubkey
from app.routers.admin.router import verify_admin_access
from app.services import compromised_service as svc
from app.services.compromised_service import (
    Observer,
    parse_event_id,
    parse_pubkey,
    select_observers,
    sign_zero_tas,
)

NPUB = "npub18ams6ewn5aj2n3wt2qawzglx9mr4nzksxhvrdc4gzrecw7n5tvjqctp424"
HEX = "3f770d65d3a764a9c5cb503ae123e62ec7598ad035d836e2a810f3877a745b24"
PROOF = "ab" * 32
ROUTER = "app.routers.admin.compromised.router"


def test_pubkeys_and_event_ids_parse_to_hex():
    assert parse_pubkey(NPUB) == HEX
    assert parse_pubkey(f" {HEX} ") == HEX
    assert parse_event_id(PROOF) == PROOF
    with pytest.raises(ValueError):
        parse_pubkey("npub1nope")
    with pytest.raises(ValueError):
        parse_event_id("not-an-id")


def test_holds_pubkey_only_at_an_entry_boundary():
    other = "11" * 32
    assert holds_pubkey(_pack_pubkeys([other, HEX]), HEX)
    assert not holds_pubkey(_pack_pubkeys([other]), HEX)
    assert not holds_pubkey(None, HEX)
    # The account's bytes straddling two entries is not a listing of it...
    straddle = _pack_pubkeys(["00" * 16 + HEX[:32], HEX[32:] + "00" * 16])
    assert bytes.fromhex(HEX) in straddle
    assert not holds_pubkey(straddle, HEX)
    # ...but a real entry after a straddling match still counts.
    assert holds_pubkey(straddle + bytes.fromhex(HEX), HEX)


def _observer(n: int) -> Observer:
    keys = Keys.generate()
    return Observer(
        f"{n:064x}", keys.public_key().to_hex(), keys.secret_key().to_bech32()
    )


def test_select_unions_db_and_relay():
    a, b, c, d = (_observer(i) for i in range(4))
    got = select_observers(
        [a, b, c, d],
        published_by={a.pubkey, b.pubkey},
        relay_signers={b.signer, c.signer},
    )
    assert got == [(a, "db"), (b, "both"), (c, "relay")]


def test_zero_ta_is_signed_by_each_observer_at_rank_zero():
    observers = [_observer(1), _observer(2)]

    async def collect():
        return [e async for e in sign_zero_tas(HEX, observers)]

    signed = asyncio.run(collect())
    for s, o in zip(signed, observers):
        event = Event.from_json(s.json)
        assert event.verify()
        assert event.author().to_hex() == o.signer
        assert event.kind().as_u16() == TA_KIND
        tags = {t[0]: t[1] for t in json.loads(s.json)["tags"]}
        assert tags["d"] == HEX
        assert tags["rank"] == "0"
        assert tags["followers"] == tags["reporters"] == tags["muters"] == "0"
        assert "hops" not in tags


def test_a_failed_relay_read_falls_back_to_the_db_and_says_so(monkeypatch):
    """`zero_compromised` with the relay down: the DB's selection is zeroed, and
    the report flags the read so the script exits non-zero."""
    a, b = _observer(1), _observer(2)
    sent = []

    async def _publish(url, events):
        sent.extend([e async for e in events])
        return SimpleNamespace(n_events=len(sent), n_acked=len(sent), aborted=False, n_unacked=0)

    class _Db:
        async def commit(self):
            pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield _Db()

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "load_observers", AsyncMock(return_value=[a, b]))
    monkeypatch.setattr(
        svc, "select_observers_publishing_on_db", AsyncMock(return_value={a.pubkey})
    )
    monkeypatch.setattr(svc, "fetch_ta_signers", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "publish_events", _publish)
    monkeypatch.setattr(svc, "clear_neo4j_scores", AsyncMock(return_value=3))
    monkeypatch.setattr(
        svc, "remove_pubkey_from_whitelists_on_db", AsyncMock(return_value=2)
    )

    report = asyncio.run(svc.zero_compromised(HEX))

    assert report.relay_read is False
    assert report.observers == [(a.pubkey, a.signer, "db")]
    assert (report.published, report.acked, report.complete) == (1, 1, True)
    assert (report.neo4j_cleared, report.whitelists_updated) == (3, 2)


def test_a_dry_run_publishes_and_clears_nothing(monkeypatch):
    a = _observer(1)
    publish = AsyncMock()
    clear = AsyncMock()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "load_observers", AsyncMock(return_value=[a]))
    monkeypatch.setattr(
        svc, "select_observers_publishing_on_db", AsyncMock(return_value={a.pubkey})
    )
    monkeypatch.setattr(svc, "fetch_ta_signers", AsyncMock(return_value=set()))
    monkeypatch.setattr(svc, "publish_events", publish)
    monkeypatch.setattr(svc, "clear_neo4j_scores", clear)

    report = asyncio.run(svc.zero_compromised(HEX, apply=False))

    assert len(report.observers) == 1
    publish.assert_not_awaited()
    clear.assert_not_awaited()


# --- /admin/compromised ---------------------------------------------------------


def _row(**kw):
    return SimpleNamespace(
        pubkey=HEX,
        proof_event_id=PROOF,
        reason=kw.get("reason"),
        added_by=kw.get("added_by"),
        created_at=datetime(2026, 11, 16),
        updated_at=datetime(2026, 11, 16),
    )


@pytest.fixture
def admin_client(client):
    from app.api import app

    app.dependency_overrides[verify_admin_access] = lambda: None
    yield client


def test_put_records_the_account_and_zeroes_it_after_answering(
    admin_client, caller, monkeypatch
):
    add = AsyncMock(return_value=_row(reason="leaked nsec", added_by=caller.pubkey))
    zero = AsyncMock()
    monkeypatch.setattr(f"{ROUTER}.add_compromised", add)
    monkeypatch.setattr(f"{ROUTER}.zero_compromised_in_background", zero)

    response = admin_client.put(
        f"/admin/compromised/{NPUB}",
        json={"proof_event_id": PROOF, "reason": "leaked nsec"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["pubkey"] == HEX
    add.assert_awaited_once_with(HEX, PROOF, "leaked nsec", caller.pubkey)
    zero.assert_awaited_once_with(HEX)


def test_put_without_a_valid_proof_is_a_422(admin_client, monkeypatch):
    add = AsyncMock()
    monkeypatch.setattr(f"{ROUTER}.add_compromised", add)

    for body in ({}, {"proof_event_id": ""}, {"proof_event_id": "nope"}):
        assert admin_client.put(f"/admin/compromised/{HEX}", json=body).status_code == 422
    assert admin_client.put(
        "/admin/compromised/npub1nope", json={"proof_event_id": PROOF}
    ).status_code == 422
    add.assert_not_awaited()


def test_get_lists_the_accounts(admin_client, monkeypatch):
    monkeypatch.setattr(f"{ROUTER}.list_compromised", AsyncMock(return_value=[_row()]))

    response = admin_client.get("/admin/compromised")

    assert response.status_code == 200
    assert [r["pubkey"] for r in response.json()] == [HEX]


def test_delete_removes_or_404s(admin_client, monkeypatch):
    remove = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(f"{ROUTER}.remove_compromised", remove)

    assert admin_client.delete(f"/admin/compromised/{NPUB}").status_code == 204
    assert admin_client.delete(f"/admin/compromised/{HEX}").status_code == 404
    assert remove.await_args_list[0].args == (HEX,)


def test_non_admin_cannot_mark_an_account_403(client, monkeypatch):
    add = AsyncMock()
    monkeypatch.setattr(f"{ROUTER}.add_compromised", add)

    response = client.put(f"/admin/compromised/{HEX}", json={"proof_event_id": PROOF})

    assert response.status_code == 403
    add.assert_not_awaited()
