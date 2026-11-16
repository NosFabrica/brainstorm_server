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
from fastapi import HTTPException
from nostr_sdk import Event, Keys

from app.core.config import settings

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
        proof_event={"id": PROOF},
        successor_pubkey=kw.get("successor_pubkey"),
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
    assert response.json()["proof_event"]["id"] == PROOF
    pubkey, body, added_by = add.await_args.args
    assert (pubkey, added_by) == (HEX, caller.pubkey)
    assert body.model_fields_set == {"proof_event_id", "reason"}
    zero.assert_awaited_once_with(HEX)


def test_put_rejects_bad_fields_with_a_422(admin_client, monkeypatch):
    add = AsyncMock()
    monkeypatch.setattr(f"{ROUTER}.add_compromised", add)

    for body in (
        {"proof_event_id": "nope"},
        {"successor_pubkey": "npub1nope"},
        {"proof_event_id": PROOF, "proof_event": {"id": PROOF}},
    ):
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


# --- the proof and the merge -------------------------------------------------


def _signed(keys: Keys, content: str = "my key was stolen") -> dict:
    from nostr_sdk import EventBuilder

    return json.loads(EventBuilder.text_note(content).sign_with_keys(keys).as_json())


def test_a_proof_event_is_kept_whole_after_its_signature_is_checked():
    event = _signed(Keys.generate())
    assert svc.verified_proof(event) == event

    forged = {**event, "content": "something else"}
    with pytest.raises(HTTPException) as e:
        svc.verified_proof(forged)
    assert e.value.status_code == 422


def test_an_nevent_pointer_keeps_its_relay_hints():
    from nostr_sdk import EventId, Nip19Event

    nevent = Nip19Event(EventId.parse(PROOF), None, None, ["wss://relay.example"]).to_bech32()
    assert svc.proof_pointer(nevent) == (PROOF, ["wss://relay.example"])
    assert svc.proof_pointer(PROOF) == (PROOF, [])


def test_a_proof_is_fetched_from_the_first_relay_holding_a_valid_copy(monkeypatch):
    event = _signed(Keys.generate())
    answers = {
        "wss://hint": RuntimeError("down"),
        settings.nostr_transfer_from_relay: {**event, "sig": "00" * 64},  # forged
        settings.nostr_transfer_to_relay: event,
    }
    asked = []

    async def _fetch(relay, flt):
        asked.append(relay)
        assert flt["ids"] == [event["id"]]
        answer = answers.get(relay)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(svc, "fetch_latest", _fetch)

    got = asyncio.run(svc.fetch_proof_event(event["id"], ["wss://hint"]))

    assert got == event
    assert asked[0] == "wss://hint"


def test_a_proof_no_relay_holds_is_a_422(monkeypatch):
    monkeypatch.setattr(svc, "fetch_latest", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as e:
        asyncio.run(svc.fetch_proof_event(PROOF, []))
    assert e.value.status_code == 422


@pytest.fixture
def stored_rows(monkeypatch):
    """`add_compromised` against an in-memory table."""
    from contextlib import asynccontextmanager

    rows: dict[str, SimpleNamespace] = {}

    class _Db:
        async def commit(self):
            pass

        async def refresh(self, row):
            pass

    @asynccontextmanager
    async def _session():
        yield _Db()

    async def _select(db, pubkey):
        return rows.get(pubkey)

    async def _upsert(db, pubkey, proof, successor, reason, added_by):
        rows[pubkey] = SimpleNamespace(
            pubkey=pubkey,
            proof_event=proof,
            proof_event_id=proof["id"],
            successor_pubkey=successor,
            reason=reason,
            added_by=added_by,
        )
        return rows[pubkey]

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "select_compromised_on_db", _select)
    monkeypatch.setattr(svc, "upsert_compromised_on_db", _upsert)
    return rows


def _add(body: dict, admin: str = "f" * 64):
    from app.schemas.request_body_schemas import CompromisedPubkeyBody

    return asyncio.run(
        svc.add_compromised(HEX, CompromisedPubkeyBody.model_validate(body), admin)
    )


def test_a_new_account_needs_a_proof(stored_rows):
    with pytest.raises(HTTPException) as e:
        _add({"reason": "stolen"})
    assert e.value.status_code == 422
    assert stored_rows == {}


def test_a_later_put_keeps_what_it_leaves_out(stored_rows):
    """The successor arrives later, without resending a proof that may be deleted
    by then; a null clears it."""
    event = _signed(Keys.generate())
    _add({"proof_event": event, "reason": "stolen"})
    successor = Keys.generate().public_key()

    row = _add({"successor_pubkey": successor.to_bech32()})
    assert row.proof_event == event
    assert row.reason == "stolen"
    assert row.successor_pubkey == successor.to_hex()

    row = _add({"successor_pubkey": None})
    assert row.successor_pubkey is None
    assert row.proof_event == event


# --- the public check -------------------------------------------------------------

PUBLIC = "app.routers.compromised.router"


@pytest.fixture
def anonymous():
    """No token at all: the check is public."""
    from fastapi.testclient import TestClient

    from app.api import app

    return TestClient(app)


@pytest.fixture
def marked(monkeypatch):
    """`check_compromised` over one marked account, the DB read faked."""
    row = SimpleNamespace(
        pubkey=HEX,
        created_at=datetime(2026, 11, 16),
        proof_event_id=PROOF,
        proof_event={"id": PROOF, "content": "my key was stolen"},
        successor_pubkey="c" * 64,
    )
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    async def _select(db, pubkeys):
        return [row] if HEX in pubkeys else []

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "select_compromised_in_on_db", _select)
    return row


def test_the_ui_can_check_one_account_without_logging_in(anonymous, marked):
    data = anonymous.get(f"/compromised/{NPUB}").json()["data"]

    assert data["pubkey"] == HEX
    assert data["compromised"] is True
    assert data["proof_event"]["content"] == "my key was stolen"
    assert data["successor_pubkey"] == "c" * 64
    assert data["since"].startswith("2026-11-16")
    assert "reason" not in data and "added_by" not in data


def test_an_unmarked_account_reads_false(anonymous, marked):
    data = anonymous.get(f"/compromised/{'1' * 64}").json()["data"]

    assert data == {
        "pubkey": "1" * 64,
        "compromised": False,
        "since": None,
        "proof_event_id": None,
        "proof_event": None,
        "successor_pubkey": None,
    }


def test_a_batch_answers_in_the_order_sent(anonymous, marked):
    response = anonymous.post("/compromised", json={"pubkeys": ["1" * 64, NPUB]})

    results = response.json()["data"]["results"]
    assert [(r["pubkey"], r["compromised"]) for r in results] == [
        ("1" * 64, False),
        (HEX, True),
    ]


def test_bad_pubkeys_and_oversized_batches_are_refused(anonymous, marked):
    assert anonymous.get("/compromised/npub1nope").status_code == 422
    assert anonymous.post("/compromised", json={"pubkeys": []}).status_code == 422
    assert (
        anonymous.post("/compromised", json={"pubkeys": [HEX, "nope"]}).status_code
        == 422
    )
    assert (
        anonymous.post("/compromised", json={"pubkeys": [HEX] * 501}).status_code
        == 413
    )
