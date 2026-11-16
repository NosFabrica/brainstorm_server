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
    sign_zero_tas,
)

NPUB = "npub18ams6ewn5aj2n3wt2qawzglx9mr4nzksxhvrdc4gzrecw7n5tvjqctp424"
HEX = "3f770d65d3a764a9c5cb503ae123e62ec7598ad035d836e2a810f3877a745b24"
PROOF = "ab" * 32
ROUTER = "app.routers.admin.compromised.router"


def test_pubkeys_and_event_ids_parse_to_hex():
    from nostr_sdk import EventId, Nip19Event

    assert parse_pubkey(NPUB) == HEX
    assert parse_pubkey(f" {HEX} ") == HEX
    assert parse_event_id(PROOF) == PROOF
    assert parse_event_id(EventId.parse(PROOF).to_bech32()) == PROOF  # note1…
    # nevent1…, which nostr-sdk's EventId.parse refuses: Derek's real proof pointer.
    real = (
        "nevent1qgsr7acdvhf6we9fch94qwhpy0nza36e3tgrtkpku25ppuu80f69kfqqyqqqqpwtucd5"
        "rjwqyldad25fekx5zxf67wslhzwl6qahmpnvmv6mu40hxx9"
    )
    assert parse_event_id(real) == (
        "000005cbe61b41c9c027dbd6aa89cd8d41193af3a1fb89dfd03b7d866cdb35be"
    )
    assert parse_event_id(Nip19Event(EventId.parse(PROOF), None, None, []).to_bech32()) == PROOF
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


def _keys(n: int = 3) -> list[Keys]:
    return [Keys.generate() for _ in range(n)]


def test_zero_ta_is_signed_by_each_observer_at_rank_zero():
    keys = _keys(2)

    async def collect():
        signers = [(k, k.public_key().to_hex()) for k in keys]
        return [e async for e in sign_zero_tas(HEX, signers)]

    signed = asyncio.run(collect())
    for s_, k in zip(signed, keys):
        event = Event.from_json(s_.json)
        assert event.verify()
        assert event.author().to_hex() == k.public_key().to_hex()
        assert event.kind().as_u16() == TA_KIND
        tags = {t[0]: t[1] for t in json.loads(s_.json)["tags"]}
        assert tags["d"] == HEX
        assert tags["rank"] == "0"
        assert tags["followers"] == tags["reporters"] == tags["muters"] == "0"
        assert "hops" not in tags


class _Db:
    async def commit(self):
        pass


@pytest.fixture
def zeroing(monkeypatch):
    """`zero_compromised` with three Observers, the DB and relay faked; records
    which nsecs were decrypted, whether the blob scan ran, and what was recorded."""
    from contextlib import asynccontextmanager

    keys = {f"{n:064x}": k for n, k in enumerate(_keys(3))}
    signer = {pk: k.public_key().to_hex() for pk, k in keys.items()}
    state = SimpleNamespace(
        keys=keys,
        signer=signer,
        relay=set(),  # signers the relay holds a TA from; None = relay down
        db_published=set(),
        decrypted=[],
        scanned=0,
        sent=[],
        recorded=[],
    )

    @asynccontextmanager
    async def _session():
        yield _Db()

    async def _signers(db, observer=None):
        rows = [(pk, s) for pk, s in signer.items()]
        return [r for r in rows if observer in (None, r[0])]

    async def _nsecs(db, observers):
        state.decrypted.extend(observers)
        return [(pk, keys[pk].secret_key().to_bech32()) for pk in observers]

    async def _scan(db, pubkey, observer=None):
        state.scanned += 1
        return set(state.db_published)

    async def _fetch(url, pubkey, signers):
        return None if state.relay is None else set(state.relay) & set(signers)

    async def _publish(url, events):
        state.sent.extend([e async for e in events])
        n = len(state.sent)
        return SimpleNamespace(
            n_events=n, n_acked=n, aborted=False, n_unacked=0, failure_samples=[]
        )

    async def _record(db, pubkey, report):
        state.recorded.append(report)

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "select_observer_signers_on_db", _signers)
    monkeypatch.setattr(svc, "select_observer_nsecs_on_db", _nsecs)
    monkeypatch.setattr(svc, "select_observers_publishing_on_db", _scan)
    monkeypatch.setattr(svc, "fetch_ta_signers", _fetch)
    monkeypatch.setattr(svc, "publish_events", _publish)
    monkeypatch.setattr(svc, "clear_neo4j_scores", AsyncMock(return_value=3))
    monkeypatch.setattr(svc, "cut_outgoing_ratings", AsyncMock(return_value=4))
    monkeypatch.setattr(
        svc, "remove_pubkey_from_whitelists_on_db", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(svc, "record_zero_report_on_db", _record)
    return state


def test_the_relay_decides_and_only_the_holders_keys_are_decrypted(zeroing):
    """No blob scan when the relay answers, and the signers come from the stored
    `assistant_pubkey`: only the one Observer that holds a TA has its nsec read."""
    a, b, c = sorted(zeroing.keys)
    zeroing.relay = {zeroing.signer[b]}

    report = asyncio.run(svc.zero_compromised(HEX))

    assert report.observers == [(b, zeroing.signer[b], "relay")]
    assert zeroing.scanned == 0
    assert zeroing.decrypted == [b]
    assert [json.loads(e.json)["pubkey"] for e in zeroing.sent] == [zeroing.signer[b]]
    assert zeroing.recorded == [
        {
            "observers": 1,
            "relay_read": True,
            "published": 1,
            "acked": 1,
            "complete": True,
            "neo4j_cleared": 3,
            "whitelists_updated": 2,
            "edges_cut": 4,
        }
    ]


def test_a_failed_relay_read_falls_back_to_the_db_and_records_it(zeroing):
    a, b, c = sorted(zeroing.keys)
    zeroing.relay = None
    zeroing.db_published = {a}

    report = asyncio.run(svc.zero_compromised(HEX))

    assert zeroing.scanned == 1
    assert report.relay_read is False
    assert report.observers == [(a, zeroing.signer[a], "db")]
    assert zeroing.recorded[0]["relay_read"] is False


def test_a_dry_run_publishes_clears_and_records_nothing(zeroing):
    zeroing.relay = set(zeroing.signer.values())

    report = asyncio.run(svc.zero_compromised(HEX, apply=False))

    assert len(report.observers) == 3
    assert zeroing.sent == [] and zeroing.decrypted == [] and zeroing.recorded == []


def test_a_background_failure_is_recorded_on_the_row(zeroing, monkeypatch):
    monkeypatch.setattr(svc, "load_observers", AsyncMock(side_effect=RuntimeError("db down")))

    asyncio.run(svc.zero_compromised_in_background(HEX))

    assert zeroing.recorded == [{"error": "RuntimeError('db down')"}]


def test_a_row_without_a_stored_signer_decrypts_just_that_row(monkeypatch):
    keys = _keys(2)
    rows = [("a" * 64, keys[0].public_key().to_hex()), ("b" * 64, None)]
    decrypted = []

    async def _nsecs(db, observers):
        decrypted.extend(observers)
        return [("b" * 64, keys[1].secret_key().to_bech32())] if observers else []

    monkeypatch.setattr(svc, "select_observer_signers_on_db", AsyncMock(return_value=rows))
    monkeypatch.setattr(svc, "select_observer_nsecs_on_db", _nsecs)

    observers = asyncio.run(svc.load_observers(None, None))

    assert decrypted == ["b" * 64]
    assert [o.signer for o in observers] == [k.public_key().to_hex() for k in keys]


def _relay_answering(per_req: int):
    """A relay that answers each REQ with `per_req` events from its authors,
    repeating authors when asked for more than it has (stored duplicates)."""
    import websockets

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] != "REQ":
                continue
            authors = msg[2]["authors"]
            for n in range(min(per_req(authors), msg[2]["limit"])):
                event = {"pubkey": authors[n % len(authors)]}
                await ws.send(json.dumps(["EVENT", msg[1], event]))
            await ws.send(json.dumps(["EOSE", msg[1]]))

    return websockets.serve(handler, "127.0.0.1", 0)


def test_a_relay_answer_that_fills_the_limit_reads_as_truncated():
    signers = [f"{n:064x}" for n in range(3)]

    async def read(per_req):
        async with _relay_answering(per_req) as server:
            port = server.sockets[0].getsockname()[1]
            return await svc.fetch_ta_signers(f"ws://127.0.0.1:{port}", HEX, signers)

    assert asyncio.run(read(lambda authors: len(authors))) == set(signers)
    assert asyncio.run(read(lambda authors: 2 * len(authors))) is None


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
    admin_client, caller, zeroing, monkeypatch
):
    """The real route and the real zeroing (DB and relay faked): PUT answers at
    once, pending, and the zeroing it started then has every Observer the relay
    says holds a TA about the account publish its rank-0 replacement."""
    import time

    a, b, c = sorted(zeroing.keys)
    zeroing.relay = {zeroing.signer[a], zeroing.signer[c]}
    rows = {}

    async def _add(pubkey, body, added_by):
        rows[pubkey] = _row(reason=body.reason, added_by=added_by)
        return rows[pubkey]

    async def _get(pubkey):
        row = rows[pubkey]
        row.last_zero_report = zeroing.recorded[-1] if zeroing.recorded else None
        row.last_zeroed_at = datetime(2026, 11, 16, 0, 0, 5) if zeroing.recorded else None
        return row

    monkeypatch.setattr(f"{ROUTER}.add_compromised", _add)
    monkeypatch.setattr(f"{ROUTER}.get_compromised", _get)

    response = admin_client.put(
        f"/admin/compromised/{NPUB}",
        json={"proof_event_id": PROOF, "reason": "leaked nsec"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["pubkey"] == HEX

    deadline = time.monotonic() + 5
    while not zeroing.recorded and time.monotonic() < deadline:
        time.sleep(0.01)
    report = zeroing.recorded[-1]
    assert report["complete"] is True and report["acked"] == 2
    published = [json.loads(e.json) for e in zeroing.sent]
    assert sorted(e["pubkey"] for e in published) == sorted(
        [zeroing.signer[a], zeroing.signer[c]]
    )
    for event in published:
        tags = {t[0]: t[1] for t in event["tags"]}
        assert (event["kind"], tags["d"], tags["rank"]) == (TA_KIND, HEX, "0")


def test_put_answers_before_a_slow_zeroing_ends(admin_client, monkeypatch):
    """A zeroing over every Observer can outlast any HTTP timeout: PUT answers
    with the row as it stands (pending) and the zeroing keeps going."""
    started, release = [], asyncio.Event()

    async def _slow(pubkey):
        started.append(pubkey)
        await release.wait()

    monkeypatch.setattr(svc, "zero_compromised", _slow)
    monkeypatch.setattr(f"{ROUTER}.add_compromised", AsyncMock(return_value=_row()))
    monkeypatch.setattr(f"{ROUTER}.get_compromised", AsyncMock(return_value=_row()))

    response = admin_client.put(f"/admin/compromised/{HEX}", json={"proof_event_id": PROOF})

    assert response.status_code == 200
    assert started == [HEX]
    assert response.json()["last_zero_report"] is None


def test_zero_now_joins_a_run_under_way_and_waits_at_most_its_limit(monkeypatch):
    calls, release = [], None

    async def _slow(pubkey):
        calls.append(pubkey)
        await release.wait()

    monkeypatch.setattr(svc, "zero_compromised", _slow)

    async def body():
        nonlocal release
        release = asyncio.Event()
        assert await svc.zero_now(HEX, wait_seconds=0.01) is False
        assert await svc.zero_now(HEX, wait_seconds=0.01) is False  # joins, no second run
        release.set()
        assert await svc.zero_now(HEX, wait_seconds=1) is True
        await asyncio.sleep(0)
        assert HEX not in svc._running

    asyncio.run(body())
    assert calls == [HEX]


def test_the_retry_loop_reruns_what_is_due(monkeypatch):
    from datetime import timedelta

    due = AsyncMock(return_value=[HEX, "b" * 64])
    ran = []

    async def _zero_now(pubkey, wait_seconds):
        ran.append((pubkey, wait_seconds))
        return True

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield None

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "select_compromised_due_on_db", due)
    monkeypatch.setattr(svc, "zero_now", _zero_now)

    n = asyncio.run(svc.retry_due_zeroings(timedelta(minutes=2), timedelta(minutes=10)))

    assert n == 2
    assert ran == [(HEX, None), ("b" * 64, None)]


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


def test_a_relay_that_never_answers_does_not_hold_up_the_proof(monkeypatch):
    event = _signed(Keys.generate())

    async def _fetch(relay, flt):
        if relay == "wss://hint":
            await asyncio.sleep(3600)  # hangs: asked alongside the others, not first
        return event if relay == settings.nostr_transfer_to_relay else None

    monkeypatch.setattr(svc, "fetch_latest", _fetch)

    async def body():
        return await asyncio.wait_for(svc.fetch_proof_event(event["id"], ["wss://hint"]), 1)

    assert asyncio.run(body()) == event


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

    async def _upsert(
        db,
        pubkey,
        proof,
        successor,
        reason,
        added_by,
        successor_event_id=None,
        compromised_since=None,
    ):
        rows[pubkey] = SimpleNamespace(
            pubkey=pubkey,
            proof_event=proof,
            proof_event_id=proof["id"],
            successor_pubkey=successor,
            successor_event_id=successor_event_id,
            compromised_since=compromised_since,
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
        successor_event_id=None,
        compromised_since=None,
    )
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    async def _select(db, pubkeys):
        return [row] if HEX in pubkeys else []

    async def _by_successor(db, pubkeys):
        return [row] if row.successor_pubkey in pubkeys else []

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "select_compromised_in_on_db", _select)
    monkeypatch.setattr(svc, "select_compromised_by_successor_on_db", _by_successor)
    return row


def test_the_ui_can_check_one_account_without_logging_in(anonymous, marked):
    data = anonymous.get(f"/compromised/{NPUB}").json()["data"]

    assert data["pubkey"] == HEX
    assert data["compromised"] is True
    assert data["proof_event"]["content"] == "my key was stolen"
    assert data["successor_pubkey"] == "c" * 64
    assert data["since"].startswith("2026-11-16")
    assert "reason" not in data and "added_by" not in data


def test_a_successor_reads_the_account_it_replaces(anonymous, marked):
    data = anonymous.post("/compromised", json={"pubkeys": ["c" * 64, HEX]}).json()["data"]
    successor, old = data["results"]

    assert successor["compromised"] is False
    assert successor["replaces"]["pubkey"] == HEX
    assert successor["replaces"]["compromised"] is True
    assert successor["replaces"]["proof_event"]["content"] == "my key was stolen"
    assert old["replaces"] is None


def test_an_unmarked_account_reads_false(anonymous, marked):
    data = anonymous.get(f"/compromised/{'1' * 64}").json()["data"]

    assert data == {
        "pubkey": "1" * 64,
        "compromised": False,
        "since": None,
        "proof_event_id": None,
        "proof_event": None,
        "successor_pubkey": None,
        "successor_event_id": None,
        "replaces": None,
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



# --- what a compromised key rates stops existing ----------------------------------


def test_marking_cuts_the_keys_own_ratings_everywhere_the_worker_reads(monkeypatch):
    """Neo4j edges, the reverse sets per target and its forward `designates:` set."""

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    cut = {"FOLLOWS": ["s1", "s2"], "MUTES": ["m1"], "REPORTS": ["r1"], "DESIGNATES": ["p1"]}
    calls = []

    async def _reverse(prefix, publisher, added_pubkeys=None, removed_pubkeys=None):
        calls.append((prefix, publisher, removed_pubkeys))

    redis = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr(svc, "neo4j_driver", SimpleNamespace(session=lambda: _Session()))
    monkeypatch.setattr(svc, "delete_outgoing_ratings", AsyncMock(return_value=cut))
    monkeypatch.setattr(svc, "update_reverse_sets", _reverse)
    monkeypatch.setattr(svc, "redis_client", redis)

    assert asyncio.run(svc.cut_outgoing_ratings(HEX)) == 5
    assert calls == [
        ("followed_by:", HEX, ["s1", "s2"]),
        ("muted_by:", HEX, ["m1"]),
        ("reported_by:", HEX, ["r1"]),
    ]
    redis.delete.assert_awaited_once_with(f"designates:{HEX}")


def test_ingest_ignores_what_a_compromised_key_publishes_about_others(monkeypatch):
    from app.message_queue_tasks import process_strfry_event as ingest

    handled = []
    for name in (
        "process_event_kind_3",
        "process_event_kind_10000",
        "process_event_kind_1984",
        "process_event_kind_5",
        "process_event_kind_10040",
    ):
        monkeypatch.setattr(
            ingest, name, AsyncMock(side_effect=lambda s, e: handled.append(e["kind"]))
        )
    monkeypatch.setattr(ingest, "compromised_authors", AsyncMock(return_value=frozenset({HEX})))

    for kind in (3, 10000, 1984, 5, 10040):
        asyncio.run(ingest.process_strfry_event(None, {"kind": kind, "pubkey": HEX}))
        asyncio.run(ingest.process_strfry_event(None, {"kind": kind, "pubkey": "1" * 64}))

    assert handled == [3, 10000, 1984, 5, 10040]  # only the live author's


def test_the_author_cache_keeps_the_last_set_when_postgres_fails(monkeypatch):
    from contextlib import asynccontextmanager

    from app.services import compromised_authors as cache

    reads = [frozenset({HEX}), RuntimeError("pg down")]

    @asynccontextmanager
    async def _session():
        yield None

    async def _select(db):
        answer = reads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return set(answer)

    monkeypatch.setattr(cache, "db_session", _session)
    monkeypatch.setattr(cache, "select_compromised_pubkeys_on_db", _select)
    monkeypatch.setattr(cache, "_cache", (0.0, frozenset()))

    assert asyncio.run(cache.compromised_authors()) == {HEX}
    assert asyncio.run(cache.compromised_authors()) == {HEX}  # cached: no read
    cache.forget_compromised_authors()
    assert asyncio.run(cache.compromised_authors()) == {HEX}  # read failed: kept


# --- metadata edits and the overview flag -------------------------------------------


def test_patch_changes_metadata_without_zeroing(admin_client, monkeypatch):
    update = AsyncMock(return_value=_row(reason="note", successor_pubkey="c" * 64))
    zero = AsyncMock()
    monkeypatch.setattr(f"{ROUTER}.update_compromised_metadata", update)
    monkeypatch.setattr(f"{ROUTER}.zero_now", zero)

    successor = Keys.generate().public_key()
    response = admin_client.patch(
        f"/admin/compromised/{NPUB}", json={"successor_pubkey": successor.to_bech32()}
    )

    assert response.status_code == 200, response.text
    pubkey, body = update.await_args.args
    assert pubkey == HEX
    assert body.model_fields_set == {"successor_pubkey"}
    assert body.successor_pubkey == successor.to_hex()
    zero.assert_not_awaited()


def _metadata(body: dict):
    from app.schemas.request_body_schemas import CompromisedMetadataBody

    return CompromisedMetadataBody.model_validate(body)


def test_metadata_update_keeps_what_it_leaves_out_and_404s_unmarked(monkeypatch):
    from contextlib import asynccontextmanager

    class _Db:
        async def commit(self):
            pass

        async def refresh(self, row):
            pass

    @asynccontextmanager
    async def _session():
        yield _Db()

    updated = []

    async def _update(db, pubkey, values):
        updated.append(values)
        return _row(**values) if pubkey == HEX else None

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "update_compromised_metadata_on_db", _update)

    asyncio.run(svc.update_compromised_metadata(HEX, _metadata({"reason": None})))
    assert updated == [{"reason": None}]  # null clears; the successor isn't touched

    with pytest.raises(HTTPException) as e:
        asyncio.run(svc.update_compromised_metadata("1" * 64, _metadata({"reason": "x"})))
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        asyncio.run(svc.update_compromised_metadata(HEX, _metadata({"successor_pubkey": HEX})))
    assert e.value.status_code == 422


def test_the_overview_says_whether_the_key_is_compromised(monkeypatch):
    from app.services import user_service

    async def _count(prefix, pubkey):
        return 0

    async def _neo(*a, **kw):
        return SimpleNamespace(
            influence=0.5, tier=None, flagged_by_observer=False, flagged_count=0,
            following=0, muting=0, reporting=0,
        )

    monkeypatch.setattr(user_service, "_redis_inbound_count", _count)
    monkeypatch.setattr(user_service, "_neo4j_outbound_counts_and_influence", _neo)
    monkeypatch.setattr(
        user_service, "compromised_authors", AsyncMock(return_value=frozenset({HEX}))
    )

    def overview(pk):
        return asyncio.run(
            user_service.get_user_overview(pk, verified_line=0.1, reporter_cutoff=0.1)
        )

    assert overview(HEX).compromised is True
    assert overview("1" * 64).compromised is False



def test_the_successor_post_is_kept_by_later_puts_and_read_publicly(
    stored_rows, monkeypatch
):
    """The new account's post about the move: set once (as an nevent here),
    stored as hex, kept by a PUT that leaves it out, and returned by the public
    check, so the UI needs no list of its own."""
    from nostr_sdk import EventId, Nip19Event

    post = "5" * 64
    nevent = Nip19Event(EventId.parse(post), None, None, []).to_bech32()
    event = _signed(Keys.generate())
    _add({"proof_event": event, "successor_event_id": nevent})
    assert stored_rows[HEX].successor_event_id == post

    _add({"reason": "later"})
    assert stored_rows[HEX].successor_event_id == post

    row = SimpleNamespace(
        **vars(stored_rows[HEX]), created_at=datetime(2026, 11, 16), compromised=True
    )

    async def _select(db, pubkeys):
        return [row]

    async def _none(db, pubkeys):
        return []

    monkeypatch.setattr(svc, "select_compromised_in_on_db", _select)
    monkeypatch.setattr(svc, "select_compromised_by_successor_on_db", _none)
    (status,) = asyncio.run(svc.check_compromised([HEX]))
    assert status.successor_event_id == post


def test_the_compromise_can_start_before_the_marking(stored_rows, monkeypatch):
    """`compromised_since` (the owner's account of when the key was taken) is
    stored as naive UTC, kept by a PUT that leaves it out, and is the public
    `since`; unset, `since` is when the account was marked."""
    since = "2026-11-15T21:00:00-03:00"
    _add({"proof_event": _signed(Keys.generate()), "compromised_since": since})
    assert stored_rows[HEX].compromised_since == datetime(2026, 11, 16)

    _add({"reason": "later"})
    assert stored_rows[HEX].compromised_since == datetime(2026, 11, 16)

    row = SimpleNamespace(**vars(stored_rows[HEX]), created_at=datetime(2026, 11, 17))

    async def _select(db, pubkeys):
        return [row]

    async def _none(db, pubkeys):
        return []

    monkeypatch.setattr(svc, "select_compromised_in_on_db", _select)
    monkeypatch.setattr(svc, "select_compromised_by_successor_on_db", _none)
    (status,) = asyncio.run(svc.check_compromised([HEX]))
    assert status.since == datetime(2026, 11, 16)

    _add({"compromised_since": None})
    row.compromised_since = stored_rows[HEX].compromised_since
    (status,) = asyncio.run(svc.check_compromised([HEX]))
    assert status.since == datetime(2026, 11, 17)


def test_patch_sets_and_clears_the_successor_post(monkeypatch):
    from contextlib import asynccontextmanager

    class _Db:
        async def commit(self):
            pass

        async def refresh(self, row):
            pass

    @asynccontextmanager
    async def _session():
        yield _Db()

    seen = []

    async def _update(db, pubkey, values):
        seen.append(values)
        return _row()

    monkeypatch.setattr(svc, "db_session", _session)
    monkeypatch.setattr(svc, "update_compromised_metadata_on_db", _update)

    asyncio.run(svc.update_compromised_metadata(HEX, _metadata({"successor_event_id": PROOF})))
    asyncio.run(svc.update_compromised_metadata(HEX, _metadata({"successor_event_id": None})))
    assert seen == [{"successor_event_id": PROOF}, {"successor_event_id": None}]

    with pytest.raises(Exception):
        _metadata({"successor_event_id": "not-an-id"})
