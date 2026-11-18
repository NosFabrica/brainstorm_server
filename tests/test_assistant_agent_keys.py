"""The Assistant's AI keys: `GET` and `POST /user/assistantAgentKeys`.

Two owners, A and B, each with an Assistant. The database is a dict from owner
to row, read through the one repo call the service makes; the relays are one
fake holding every event. Signing and NIP-44 are real.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Timestamp

from app.api import app
from app.core.database import get_db
from app.services import agent_keys_build, agent_keys_service
from app.utils import nip44
from app.utils.api_validators import verify_token
from app.utils.auth.auth_models import JWTData

OUTBOX = ["wss://one.example", "wss://two.example", "wss://three.example"]
SECRET_KEY = "sk-or-v1-0123456789abcdef"
LIST = [["key", SECRET_KEY, "https://openrouter.ai/api/v1"], ["x-unknown", "kept"]]


class Owner:
    def __init__(self) -> None:
        self.keys = Keys.generate()
        self.assistant = Keys.generate()

    @property
    def pubkey(self) -> str:
        return self.keys.public_key().to_hex()

    @property
    def assistant_pubkey(self) -> str:
        return self.assistant.public_key().to_hex()

    def seal(self, tags) -> str:
        """The owner's client: the list encrypted owner -> Assistant."""
        return nip44.encrypt(self.keys, self.assistant_pubkey, json.dumps(tags))

    def open(self, ciphertext: str) -> list:
        return json.loads(nip44.decrypt(self.keys, self.assistant_pubkey, ciphertext))


class Relays:
    """Every outbox relay, faked as one store, behind fake websockets: the
    real `RelayGroup` reads and sends through them."""

    def __init__(self) -> None:
        self.held: list[dict] = []
        self.silent: set[str] = set()  # Never connect.
        self.hanging: set[str] = set()  # Connect, never answer a REQ.
        self.refusing: set[str] = set()  # Answer an EVENT with OK false.
        self.sent: list[tuple[str, dict]] = []
        self.connects: list[str] = []
        self.closed = 0

    async def open(self, url: str):
        if url in self.silent:
            raise TimeoutError()
        self.connects.append(url)
        return _FakeSocket(self, url)

    def event_ids_sent(self) -> set[str]:
        return {event["id"] for _, event in self.sent}


class _FakeSocket:
    def __init__(self, relays: Relays, url: str) -> None:
        self.relays, self.url = relays, url
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    def _answer(self, *msg) -> None:
        self.inbox.put_nowait(json.dumps(list(msg)))

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        relays = self.relays
        if msg[0] == "REQ":
            if self.url in relays.hanging:
                return
            sub, flt = msg[1], msg[2]
            found = sorted(
                (
                    e
                    for e in relays.held
                    if e["kind"] in flt["kinds"] and e["pubkey"] in flt["authors"]
                ),
                key=lambda e: -e["created_at"],
            )
            for event in found[: flt.get("limit", len(found))]:
                self._answer("EVENT", sub, event)
            self._answer("EOSE", sub)
        elif msg[0] == "EVENT":
            event = msg[1]
            relays.sent.append((self.url, event))
            if self.url in relays.refusing:
                self._answer("OK", event["id"], False, "blocked: no")
                return
            if event["id"] not in {e["id"] for e in relays.held}:
                relays.held.append(event)
            self._answer("OK", event["id"], True, "")

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.relays.closed += 1


class LockRedis:
    """The edit lock's Redis: `SET NX EX` and the compare-and-delete script."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        assert nx and ex == agent_keys_service.LOCK_TTL_SECONDS
        if key in self.values:
            return None
        self.values[key] = value
        return True

    async def eval(self, _script, _numkeys, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


@pytest.fixture(autouse=True)
def lock_redis(monkeypatch) -> LockRedis:
    fake = LockRedis()
    monkeypatch.setattr("app.services.agent_keys_service.redis_client", fake)
    return fake


@pytest.fixture
def a() -> Owner:
    return Owner()


@pytest.fixture
def b() -> Owner:
    return Owner()


@pytest.fixture
def relays(monkeypatch) -> Relays:
    fake = Relays()
    monkeypatch.setattr("app.services.relay_session._open", fake.open)
    monkeypatch.setattr(
        "app.services.agent_keys_service.assistant_relay_list", lambda: list(OUTBOX)
    )
    return fake


@pytest.fixture
def db_rows(monkeypatch, a, b) -> dict:
    """owner -> row, as `select_brainstorm_nsec_by_pubkey_on_db` reads it."""
    rows = {
        o.pubkey: SimpleNamespace(
            pubkey=o.pubkey, nsec=o.assistant.secret_key().to_hex()
        )
        for o in (a, b)
    }
    looked_up: list[str] = []

    async def _select(_db, pubkey):
        looked_up.append(pubkey)
        if pubkey not in rows:
            raise HTTPException(status_code=404)
        return rows[pubkey]

    monkeypatch.setattr(
        "app.services.agent_keys_service.select_brainstorm_nsec_by_pubkey_on_db",
        _select,
    )
    rows["looked_up"] = looked_up  # type: ignore[assignment]
    return rows


@pytest.fixture
def as_user(relays, db_rows):
    """A client whose token is `who`'s."""
    current: dict[str, str] = {}

    async def _fake_verify_token(request: Request) -> None:
        request.state.jwt_data = JWTData(nostr_pubkey=current["pubkey"])

    async def _fake_db():
        yield MagicMock()

    app.dependency_overrides[verify_token] = _fake_verify_token
    app.dependency_overrides[get_db] = _fake_db
    test_client = TestClient(app)

    def _as(owner: Owner) -> TestClient:
        current["pubkey"] = owner.pubkey
        return test_client

    yield _as
    app.dependency_overrides.clear()


def _get(client) -> dict:
    r = client.get("/user/assistantAgentKeys")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _post(client, ciphertext: str, previous, **extra):
    return client.post(
        "/user/assistantAgentKeys",
        json={"ciphertext": ciphertext, "previousCreatedAt": previous, **extra},
    )


def _give(client, owner: Owner, tags=LIST) -> dict:
    got = _get(client)
    r = _post(client, owner.seal(tags), got["createdAt"], previousId=got["id"])
    assert r.status_code == 200, r.text
    return r.json()["data"]


# --- Reading ----------------------------------------------------------------


def test_no_list_yet_is_null_and_null(as_user, a):
    assert _get(as_user(a)) == {
        "assistant": a.assistant_pubkey,
        "ciphertext": None,
        "createdAt": None,
        "id": None,
    }


def test_a_given_list_reads_back_to_the_owner(as_user, a, relays):
    published = _give(as_user(a), a)

    got = _get(as_user(a))
    assert got["assistant"] == a.assistant_pubkey
    assert got["createdAt"] == published["createdAt"]
    assert got["id"] == published["id"] == relays.held[-1]["id"]
    assert a.open(got["ciphertext"]) == LIST


def test_the_get_decrypts_only_with_the_owners_key(as_user, a, b):
    _give(as_user(a), a)
    ciphertext = _get(as_user(a))["ciphertext"]
    stranger = Keys.generate()

    for wrong in (stranger, b.keys, b.assistant):
        with pytest.raises(nip44.Nip44Error):
            nip44.decrypt(wrong, a.assistant_pubkey, ciphertext)


def test_the_published_event_is_encrypted_to_the_assistant_itself(as_user, a, relays):
    _give(as_user(a), a)
    (event,) = relays.held
    assert event["kind"] == 12000 and event["pubkey"] == a.assistant_pubkey
    assert event["tags"] == [["client", "Brainstorm"]]
    assert SECRET_KEY not in json.dumps(event)
    own = nip44.decrypt(a.assistant, a.assistant_pubkey, event["content"])
    assert json.loads(own) == LIST
    # The owner can't read the stored copy: it is the Assistant's own.
    with pytest.raises(nip44.Nip44Error):
        nip44.decrypt(a.keys, a.assistant_pubkey, event["content"])


@pytest.mark.parametrize("method", ["get", "post"])
def test_no_token_no_answer(method):
    response = TestClient(app).request(
        method.upper(), "/user/assistantAgentKeys", json={"ciphertext": "x"}
    )
    assert response.status_code == 401


def test_the_route_is_not_the_profile_lookup(as_user, a, monkeypatch):
    profile = MagicMock(side_effect=AssertionError("hit /user/{pubkey}"))
    monkeypatch.setattr("app.routers.user.router.get_user_graph_data", profile)
    assert set(_get(as_user(a))) == {"assistant", "ciphertext", "createdAt", "id"}


def test_an_owner_with_no_assistant_is_a_404_no_assistant(as_user, db_rows, a):
    del db_rows[a.pubkey]
    r = as_user(a).get("/user/assistantAgentKeys")
    assert r.status_code == 404
    assert r.headers["X-Reason"] == "no-assistant"


# --- One owner can never reach another's -------------------------------------


def test_a_token_reads_its_own_assistant_whatever_the_request_names(
    as_user, a, b, db_rows
):
    _give(as_user(b), b, [["key", "sk-b-secret", "https://b.example/v1"]])
    client = as_user(a)

    for query in (
        {"pubkey": b.pubkey},
        {"owner": b.pubkey},
        {"assistant": b.assistant_pubkey},
        {"id": b.pubkey},
    ):
        r = client.get("/user/assistantAgentKeys", params=query)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["assistant"] == a.assistant_pubkey
        assert data["ciphertext"] is None  # A has given nothing yet.

    assert set(db_rows["looked_up"]) == {a.pubkey, b.pubkey}
    assert db_rows["looked_up"][-1] == a.pubkey


def test_a_token_writes_its_own_assistant_whatever_the_body_names(
    as_user, a, b, relays
):
    _give(as_user(b), b, [["key", "sk-b-secret", "https://b.example/v1"]])
    b_before = _get(as_user(b))

    # A's client, naming B everywhere it could.
    r = _post(
        as_user(a),
        a.seal(LIST),
        None,
        pubkey=b.pubkey,
        owner=b.pubkey,
        assistant=b.assistant_pubkey,
    )
    assert r.status_code == 200, r.text

    assert relays.sent[-1][1]["pubkey"] == a.assistant_pubkey
    b_after = _get(as_user(b))
    assert b_after["createdAt"] == b_before["createdAt"]
    assert b.open(b_after["ciphertext"]) == [
        ["key", "sk-b-secret", "https://b.example/v1"]
    ]


def test_a_list_sealed_for_another_assistant_is_refused(as_user, a, b, relays):
    """B's ciphertext (B -> B's Assistant) sent with A's token decrypts with
    neither A's Assistant nor A as peer: nothing is published."""
    r = _post(as_user(a), b.seal(LIST), None)
    assert r.status_code == 422
    assert relays.sent == []


def test_a_list_sealed_by_someone_else_to_my_assistant_is_refused(as_user, a, relays):
    """Only the owner on record can give the Assistant keys."""
    stranger = Keys.generate()
    ciphertext = nip44.encrypt(stranger, a.assistant_pubkey, json.dumps(LIST))
    r = _post(as_user(a), ciphertext, None)
    assert r.status_code == 422
    assert relays.sent == []


# --- NIP-FA's editing rules ---------------------------------------------------


def test_created_at_is_after_the_previous_version(as_user, a, relays):
    first = _give(as_user(a), a)
    future = int(time.time()) + 1000
    # Re-signed as the Assistant would have.
    relays.held[-1] = json.loads(
        EventBuilder(Kind(12000), relays.held[-1]["content"])
        .custom_created_at(Timestamp.from_secs(future))
        .sign_with_keys(a.assistant)
        .as_json()
    )
    assert first["createdAt"] < future

    second = _give(as_user(a), a, [])
    assert second["createdAt"] == future + 1


def test_a_stale_previous_created_at_is_a_409(as_user, a, relays):
    published = _give(as_user(a), a)
    r = _post(as_user(a), a.seal([]), published["createdAt"] - 1)
    assert r.status_code == 409
    r = _post(as_user(a), a.seal([]), None)
    assert r.status_code == 409
    assert len(relays.event_ids_sent()) == 1


def test_half_the_relays_answering_is_not_enough(as_user, a, relays):
    relays.silent = {OUTBOX[0], OUTBOX[1]}
    assert as_user(a).get("/user/assistantAgentKeys").status_code == 502
    assert _post(as_user(a), a.seal(LIST), None).status_code == 502
    assert relays.sent == []

    relays.silent = {OUTBOX[0]}  # Two of three is a majority.
    assert as_user(a).get("/user/assistantAgentKeys").status_code == 200


def test_fewer_than_half_accepting_is_a_502(as_user, a, relays):
    relays.refusing = {OUTBOX[0], OUTBOX[1]}
    r = _post(as_user(a), a.seal(LIST), None)
    assert r.status_code == 502


def test_the_report_names_each_relay(as_user, a, relays):
    relays.refusing = {OUTBOX[2]}
    data = _give(as_user(a), a)
    assert [(line["url"], line["ok"]) for line in data["relays"]] == [
        (OUTBOX[0], True),
        (OUTBOX[1], True),
        (OUTBOX[2], False),
    ]


def test_another_assistants_event_is_not_read_as_the_list(as_user, a, b, relays):
    """A kind 12000 that isn't the Assistant's own, validly signed, is ignored."""
    forged = json.loads(
        EventBuilder(Kind(12000), "x").sign_with_keys(b.assistant).as_json()
    )
    forged["pubkey"] = a.assistant_pubkey  # Claims A's Assistant; won't verify.
    relays.held.append(forged)
    assert _get(as_user(a))["ciphertext"] is None


@pytest.mark.parametrize(
    "tags, reason",
    [
        ([["key", SECRET_KEY, "http://openrouter.ai/api/v1"]], "https"),
        ([["key", SECRET_KEY]], "https"),
        ([["key", SECRET_KEY, "ftp://x.example"]], "https"),
        ({"key": SECRET_KEY}, "array"),
        ([["key", 1, "https://x.example"]], "strings"),
        ([[]], "strings"),
        ([["t", str(i)] for i in range(33)], "32"),
    ],
)
def test_a_list_breaking_the_rules_is_a_422_that_never_echoes_the_key(
    as_user, a, relays, tags, reason
):
    r = _post(as_user(a), a.seal(tags), None)
    assert r.status_code == 422
    assert reason in r.json()["detail"]
    assert SECRET_KEY not in r.text
    assert relays.sent == []


def test_keys_never_reach_the_logs(as_user, a, relays, app_logs):
    _give(as_user(a), a)
    _get(as_user(a))
    _post(as_user(a), a.seal([["key", SECRET_KEY, "http://bad.example"]]), None)
    relays.silent = set(OUTBOX)
    _post(as_user(a), a.seal(LIST), None)

    assert any("published 2 tags" in line for line in app_logs)  # Logs were seen.
    assert not any(SECRET_KEY in line for line in app_logs)


# --- The pure rules -----------------------------------------------------------


@pytest.mark.parametrize(
    "key, url, ok",
    [
        ("sk", "https://openrouter.ai/api/v1", True),
        ("sk", "http://openrouter.ai/api/v1", False),
        ("", "http://ollama.lan:11434/v1", True),
        ("sk", "http://localhost:11434/v1", True),
        ("sk", "http://127.0.0.5/v1", True),
        ("sk", "http://[::1]:8080/v1", True),
        ("sk", "http://10.0.0.1/v1", False),
        ("sk", "https:///nohost", False),
        ("", "ws://x.example", False),
        # Quartz's AgentKeyTag.isUrlAllowed, case for case.
        ("sk", "http://evil.example@localhost/v1", False),
        ("sk", "http://localhost@evil.example/v1", False),
        ("sk", "https://user@api.example/v1", True),
        ("sk", "https://exa mple.com/v1", False),
        ("sk", "https://api.example/v1 with space", False),
        ("sk", "https://api.example/\tv1", False),
        ("sk", "https://api.example/v1\x01", False),
        ("sk", "https://api.example/\u00a0v1", False),
        ("sk", " https://api.example/v1\n", True),  # Trimmed first, as Quartz.
        ("sk", "http://[0:0:0:0:0:0:0:1]", False),
        ("sk", "http://[0:0:0:0:0:0:0:1]:8080/v1", False),
        ("sk", "http://127.1/v1", False),
        ("sk", "http://127.0.0.256/v1", False),
        ("sk", "http://127.0.0.1.evil.example/v1", False),
        ("sk", "http://localhost.evil.example/v1", False),
        ("sk", "HTTP://LOCALHOST:11434/v1", True),
        ("", "http://[0:0:0:0:0:0:0:1]", True),
    ],
)
def test_the_nip_fa_url_rule(key, url, ok):
    assert agent_keys_build.is_key_url_allowed(key, url) is ok


def test_majority_means_more_than_half():
    assert not agent_keys_build.has_quorum(1, 2)
    assert agent_keys_build.has_quorum(2, 3)
    assert not agent_keys_build.has_quorum(0, 0)


def test_newest_breaks_a_tie_by_the_lowest_id():
    events = [
        {"created_at": 5, "id": "b"},
        {"created_at": 5, "id": "a"},
        {"created_at": 4, "id": "0"},
    ]
    assert agent_keys_build.newest(events) == {"created_at": 5, "id": "a"}


def test_an_empty_list_is_brackets():
    assert agent_keys_build.serialize([]) == "[]"


def test_the_service_holds_no_lock_after_a_failure(as_user, a, relays, lock_redis):
    relays.silent = set(OUTBOX)
    assert _post(as_user(a), a.seal(LIST), None).status_code == 502
    assert lock_redis.values == {}
    relays.silent = set()
    assert _give(as_user(a), a)["createdAt"]  # The next edit goes through.
    assert lock_redis.values == {}


def test_a_second_edit_while_one_is_under_way_is_a_409(
    as_user, a, b, relays, lock_redis
):
    """The lock is in Redis, so it holds across replicas: another replica's
    edit of A's Assistant blocks this one, and nothing is read or sent."""
    key = f"lock:assistant_agent_keys:{a.assistant_pubkey}"
    lock_redis.values[key] = "another replica's token"
    r = _post(as_user(a), a.seal(LIST), None)
    assert r.status_code == 409
    assert relays.sent == [] and relays.connects == []
    assert lock_redis.values == {key: "another replica's token"}  # Not ours to free.
    # B's Assistant has its own lock.
    assert _give(as_user(b), b)["createdAt"]


def test_an_unreachable_redis_is_a_503_and_nothing_is_sent(
    as_user, a, relays, monkeypatch
):
    from redis.exceptions import ConnectionError as RedisConnectionError

    class _Down:
        async def set(self, *_, **__):
            raise RedisConnectionError("down")

    monkeypatch.setattr("app.services.agent_keys_service.redis_client", _Down())
    assert _post(as_user(a), a.seal(LIST), None).status_code == 503
    assert relays.sent == []


# --- previousId -------------------------------------------------------------------


def test_a_previous_id_that_isnt_the_newest_is_a_409(as_user, a, relays):
    published = _give(as_user(a), a)
    # Another version, signed in the same second: the same created_at.
    other = json.loads(
        EventBuilder(Kind(12000), nip44.encrypt(a.assistant, a.assistant_pubkey, "[]"))
        .custom_created_at(Timestamp.from_secs(published["createdAt"]))
        .sign_with_keys(a.assistant)
        .as_json()
    )
    relays.held = [other]
    assert other["id"] != published["id"]

    # The createdAt alone can't tell the two apart; the id can.
    r = _post(
        as_user(a),
        a.seal([]),
        published["createdAt"],
        previousId=published["id"],
    )
    assert r.status_code == 409
    r = _post(as_user(a), a.seal([]), None, previousId=published["id"])
    assert r.status_code == 409
    assert relays.event_ids_sent() == {published["id"]}


def test_previous_id_alone_or_with_created_at_edits_the_newest(as_user, a, relays):
    first = _give(as_user(a), a)
    r = _post(as_user(a), a.seal([]), None, previousId=first["id"])
    assert r.status_code == 200, r.text
    second = r.json()["data"]
    r = _post(as_user(a), a.seal(LIST), second["createdAt"], previousId=second["id"])
    assert r.status_code == 200, r.text
    # previousCreatedAt may be left out altogether.
    third = r.json()["data"]
    r = as_user(a).post(
        "/user/assistantAgentKeys",
        json={"ciphertext": a.seal([]), "previousId": third["id"]},
    )
    assert r.status_code == 200, r.text


def test_no_previous_version_named_is_a_409_once_one_exists(as_user, a, relays):
    _give(as_user(a), a)
    r = as_user(a).post("/user/assistantAgentKeys", json={"ciphertext": a.seal([])})
    assert r.status_code == 409


@pytest.mark.parametrize("previous_id", ["xyz", "A" * 64, "0" * 63, 7])
def test_a_malformed_previous_id_is_a_422(as_user, a, relays, previous_id):
    r = _post(as_user(a), a.seal(LIST), None, previousId=previous_id)
    assert r.status_code == 422
    assert relays.sent == []


@pytest.mark.parametrize(
    "current, previous_id, previous_at, ok",
    [
        (None, None, None, True),
        (None, "a" * 64, None, False),
        (None, None, 5, False),
        ({"id": "a" * 64, "created_at": 5}, None, None, False),
        ({"id": "a" * 64, "created_at": 5}, "a" * 64, None, True),
        ({"id": "a" * 64, "created_at": 5}, None, 5, True),
        ({"id": "a" * 64, "created_at": 5}, "a" * 64, 5, True),
        ({"id": "a" * 64, "created_at": 5}, "b" * 64, 5, False),
        ({"id": "a" * 64, "created_at": 5}, "a" * 64, 4, False),
    ],
)
def test_is_expected_version(current, previous_id, previous_at, ok):
    assert agent_keys_build.is_expected_version(current, previous_id, previous_at) is ok


# --- The relays: a majority and a grace, one connect -------------------------------


def test_the_read_returns_once_a_majority_answered(as_user, a, relays, monkeypatch):
    """A relay that connects and never answers no longer holds the GET to the
    read's 5 s: two of three answered, and the grace is all that is waited."""
    monkeypatch.setattr("app.services.relay_session.QUORUM_GRACE_SECONDS", 0.05)
    _give(as_user(a), a)
    relays.hanging = {OUTBOX[2]}
    started = time.monotonic()
    got = _get(as_user(a))
    assert time.monotonic() - started < 1.5
    assert a.open(got["ciphertext"]) == LIST


def test_without_a_majority_the_read_waits_and_fails(as_user, a, relays, monkeypatch):
    monkeypatch.setattr("app.services.relay_session.READ_TIMEOUT_SECONDS", 0.3)
    relays.hanging = {OUTBOX[1], OUTBOX[2]}
    assert as_user(a).get("/user/assistantAgentKeys").status_code == 502


def test_the_post_connects_once_per_relay_and_sends_to_late_ones(
    as_user, a, relays, monkeypatch
):
    """The read and the send share one socket per relay; a relay whose answer
    wasn't waited for still gets the new version."""
    monkeypatch.setattr("app.services.relay_session.QUORUM_GRACE_SECONDS", 0.05)
    relays.hanging = {OUTBOX[2]}
    r = _post(as_user(a), a.seal(LIST), None)
    assert r.status_code == 200, r.text
    assert sorted(relays.connects) == sorted(OUTBOX)
    assert sorted(url for url, _ in relays.sent) == sorted(OUTBOX)
    assert relays.closed == len(OUTBOX)
    assert all(line["ok"] for line in r.json()["data"]["relays"])


def test_a_relay_that_cant_be_reached_is_reported_so(as_user, a, relays):
    relays.silent = {OUTBOX[1]}
    data = _give(as_user(a), a)
    assert [(line["url"], line["ok"], line["message"]) for line in data["relays"]] == [
        (OUTBOX[0], True, None),
        (OUTBOX[1], False, "couldn't connect"),
        (OUTBOX[2], True, None),
    ]


def test_relay_nostr_band_is_not_in_the_outbox():
    from app.services.assistant_profile_service import assistant_relay_list

    assert not [url for url in assistant_relay_list() if "nostr.band" in url]
