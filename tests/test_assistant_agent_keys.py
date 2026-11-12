"""The Assistant's AI keys: `GET` and `POST /user/assistantAgentKeys`.

Two owners, A and B, each with an Assistant. The database is a dict from owner
to row, read through the one repo call the service makes; the relays are one
fake holding every event. Signing and NIP-44 are real.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from nostr_sdk import Event, EventBuilder, Keys, Kind, Timestamp

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
    """Every outbox relay, faked as one store."""

    def __init__(self) -> None:
        self.held: list[dict] = []
        self.silent: set[str] = set()
        self.refusing: set[str] = set()
        self.sent: list[tuple[list[str], Event]] = []

    def client(self, *_, **__):
        relays = self

        class _FakeClient:
            def __init__(self) -> None:
                self.pending: list[str] = []

            async def add_relay(self, url):
                self.pending.append(url)

            async def try_connect(self, _timeout):
                tried, self.pending = self.pending, []
                return SimpleNamespace(success=tried, failed={})

            async def send_event_to(self, urls, event):
                relays.sent.append((list(urls), event))
                ok = [u for u in urls if u not in relays.refusing]
                if ok:
                    relays.held.append(json.loads(event.as_json()))
                return SimpleNamespace(
                    success=ok, failed={u: "blocked" for u in urls if u not in ok}
                )

            async def disconnect(self):
                pass

        return _FakeClient()

    async def fetch_latest(self, relay: str, flt: dict):
        if relay in self.silent:
            raise TimeoutError()
        found = [
            e
            for e in self.held
            if e["kind"] in flt["kinds"] and e["pubkey"] in flt["authors"]
        ]
        return max(found, key=lambda e: e["created_at"], default=None)


@pytest.fixture
def a() -> Owner:
    return Owner()


@pytest.fixture
def b() -> Owner:
    return Owner()


@pytest.fixture
def relays(monkeypatch) -> Relays:
    fake = Relays()
    monkeypatch.setattr("app.services.assistant_publish.Client", fake.client)
    monkeypatch.setattr(
        "app.services.assistant_publish.fetch_latest", fake.fetch_latest
    )
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
    previous = _get(client)["createdAt"]
    r = _post(client, owner.seal(tags), previous)
    assert r.status_code == 200, r.text
    return r.json()["data"]


# --- Reading ----------------------------------------------------------------


def test_no_list_yet_is_null_and_null(as_user, a):
    assert _get(as_user(a)) == {
        "assistant": a.assistant_pubkey,
        "ciphertext": None,
        "createdAt": None,
    }


def test_a_given_list_reads_back_to_the_owner(as_user, a, relays):
    published = _give(as_user(a), a)

    got = _get(as_user(a))
    assert got["assistant"] == a.assistant_pubkey
    assert got["createdAt"] == published["createdAt"]
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
    assert set(_get(as_user(a))) == {"assistant", "ciphertext", "createdAt"}


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

    assert relays.sent[-1][1].author().to_hex() == a.assistant_pubkey
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
    assert len(relays.sent) == 1


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


def test_the_service_holds_no_lock_after_a_failure(as_user, a, relays):
    relays.silent = set(OUTBOX)
    assert _post(as_user(a), a.seal(LIST), None).status_code == 502
    assert agent_keys_service._editing == set()


def test_a_second_edit_while_one_is_under_way_is_a_409(as_user, a, relays, monkeypatch):
    monkeypatch.setattr(agent_keys_service, "_editing", {a.assistant_pubkey})
    r = _post(as_user(a), a.seal(LIST), None)
    assert r.status_code == 409
    assert relays.sent == []
