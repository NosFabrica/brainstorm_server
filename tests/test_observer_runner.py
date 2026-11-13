"""`/internal/observerRunner/*`: the print worker's calls, as the Assistant.

Two owners, A and B, each with an Assistant; the database is a dict, read
through the one repo call per route. Signing and NIP-44 are real.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from nostr_sdk import Event, EventBuilder, Keys, Kind, Tag

from app.api import app
from app.core.config import settings
from app.core.database import get_db
from app.internal_api import internal_app
from app.utils import nip44

TOKEN = "runner-token-0123456789abcdef-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SCHEDULE = '[["task","observer"],["param","key","https://openrouter.ai/api/v1","a3f9"]]'
AI_KEYS = '[["key","sk-or-v1-very-secret","https://openrouter.ai/api/v1"]]'


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


@pytest.fixture
def a() -> Owner:
    return Owner()


@pytest.fixture
def b() -> Owner:
    return Owner()


@pytest.fixture
def rows(monkeypatch, a, b) -> dict:
    table = {
        o.pubkey: SimpleNamespace(
            pubkey=o.pubkey, nsec=o.assistant.secret_key().to_hex()
        )
        for o in (a, b)
    }

    async def _select(_db, pubkey):
        if pubkey not in table:
            raise HTTPException(status_code=404)
        return table[pubkey]

    monkeypatch.setattr(
        "app.services.agent_keys_service.select_brainstorm_nsec_by_pubkey_on_db",
        _select,
    )
    return table


@pytest.fixture
def runner(monkeypatch, rows):
    monkeypatch.setattr(settings, "observer_runner_token", TOKEN)

    async def _fake_db():
        yield MagicMock()

    internal_app.dependency_overrides[get_db] = _fake_db
    yield TestClient(internal_app)
    internal_app.dependency_overrides.pop(get_db, None)


def _decrypt(client, owner: str, event: dict, headers=AUTH, schedule=None):
    body = {"owner": owner, "event": event}
    if schedule is not None:
        body["schedule"] = schedule
    return client.post("/internal/observerRunner/decrypt", json=body, headers=headers)


def _event(signer: Keys, kind: int, content: str, tags=()) -> dict:
    return json.loads(
        EventBuilder(Kind(kind), content)
        .tags([Tag.parse(list(t)) for t in tags])
        .allow_self_tagging()
        .sign_with_keys(signer)
        .as_json()
    )


def _schedule(owner: "Owner", runner_pubkey: str | None = None, tags=None) -> dict:
    """The owner's kind 34817, encrypted owner -> their Assistant, naming it."""
    p = runner_pubkey or owner.assistant_pubkey
    return _event(
        owner.keys,
        34817,
        nip44.encrypt(owner.keys, owner.assistant_pubkey, SCHEDULE),
        tags if tags is not None else [["d", "observer"], ["p", p]],
    )


def _ai_keys(owner: "Owner") -> dict:
    """The Assistant's own kind 12000, encrypted to itself."""
    return _event(
        owner.assistant,
        12000,
        nip44.encrypt(owner.assistant, owner.assistant_pubkey, AI_KEYS),
    )


def _sign(
    client, owner: str, kind: int, tags, created_at=None, content="", headers=AUTH
):
    return client.post(
        "/internal/observerRunner/sign",
        json={
            "owner": owner,
            "event": {
                "kind": kind,
                "created_at": created_at
                if created_at is not None
                else int(time.time()),
                "tags": tags,
                "content": content,
            },
        },
        headers=headers,
    )


def _site_tags(owner: str) -> list[list[str]]:
    return [
        ["d", "observer"],
        ["p", owner],
        ["path", "/index.html", "ab" * 32],
        ["title", "The Nostr Observer"],
    ]


def _upload_tags(expiration: int) -> list[list[str]]:
    return [["t", "upload"], ["x", "cd" * 32], ["expiration", str(expiration)]]


ROUTES = [
    ("GET", "/internal/observerRunner/assistants", None),
    (
        "POST",
        "/internal/observerRunner/decrypt",
        {"owner": "0" * 64, "event": {}},
    ),
    ("POST", "/internal/observerRunner/sign", {"owner": "0" * 64, "event": {}}),
]


# --- The token ----------------------------------------------------------------


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_no_token_configured_is_a_404_even_with_a_token(
    runner, monkeypatch, method, path, body
):
    monkeypatch.setattr(settings, "observer_runner_token", "")
    for headers in ({}, AUTH, {"Authorization": "Bearer "}):
        r = runner.request(method, path, json=body, headers=headers)
        assert r.status_code == 404, (headers, r.text)
        assert r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("method, path, body", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"access_token": TOKEN},
    ],
)
def test_a_missing_or_wrong_token_is_a_404(runner, method, path, body, headers):
    r = runner.request(method, path, json=body, headers=headers)
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path", ["/internal/observerRunner/decrypt", "/internal/observerRunner/sign"]
)
def test_a_malformed_body_without_the_token_is_a_404_not_a_422(runner, path):
    r = runner.post(
        path, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 404


def test_the_token_is_compared_in_constant_time(runner, monkeypatch):
    calls: list[tuple[bytes, bytes]] = []

    def _spy(given, expected):
        calls.append((given, expected))
        return given == expected

    monkeypatch.setattr("app.routers.observer_runner.guard.hmac.compare_digest", _spy)
    runner.get(
        "/internal/observerRunner/assistants", headers={"Authorization": "Bearer nope"}
    )
    assert calls == [(b"nope", TOKEN.encode())]


def test_the_routes_are_not_in_the_public_api_document(runner):
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/internal")]
    assert runner.get("/openapi.json", headers=AUTH).status_code == 404
    assert runner.get("/docs", headers=AUTH).status_code == 404


# --- The public app: /internal/ doesn't exist there --------------------------------


@pytest.mark.parametrize("method, path, body", ROUTES)
@pytest.mark.parametrize("suffix", ["", "/"])
@pytest.mark.parametrize("verb", ["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def test_the_public_app_answers_404_for_internal_even_with_the_token(
    runner, method, path, body, suffix, verb
):
    public = TestClient(app, follow_redirects=False)
    r = public.request(verb, path + suffix, json=body, headers=AUTH)
    assert r.status_code == 404, (verb, path, r.status_code)
    assert r.json() == {"detail": "Not Found"}
    assert "allow" not in r.headers and "location" not in r.headers


@pytest.mark.parametrize(
    "path",
    ["/internal", "/internal/", "//internal/observerRunner/assistants", "/INTERNAL/x"],
)
def test_the_public_app_refuses_any_internal_spelling(runner, path):
    r = TestClient(app, follow_redirects=False).get(path, headers=AUTH)
    assert r.status_code == 404


def test_the_public_app_still_serves_its_own_routes(runner):
    assert TestClient(app).get("/health").json() == 1


# --- Before routing: no 405, no redirect, no 422 -----------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/internal/observerRunner/decrypt"),
        ("GET", "/internal/observerRunner/sign"),
        ("POST", "/internal/observerRunner/assistants"),
        ("DELETE", "/internal/observerRunner/assistants"),
        ("PUT", "/internal/observerRunner/sign"),
    ],
)
def test_a_wrong_method_without_the_token_is_a_404_not_a_405(runner, method, path):
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        r = runner.request(method, path, headers=headers)
        assert r.status_code == 404, r.status_code
        assert "allow" not in r.headers
        assert r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/internal/observerRunner/assistants/"),
        ("POST", "/internal/observerRunner/decrypt/"),
        ("POST", "/internal/observerRunner/sign/"),
    ],
)
def test_a_trailing_slash_is_a_404_not_a_redirect(runner, method, path):
    client = TestClient(internal_app, follow_redirects=False)
    for headers in ({}, AUTH):
        r = client.request(method, path, json={}, headers=headers)
        assert r.status_code == 404, (headers, r.status_code)
        assert "location" not in r.headers


def test_a_wrong_method_with_the_token_is_a_405(runner):
    r = runner.get("/internal/observerRunner/sign", headers=AUTH)
    assert r.status_code == 405


def test_any_other_path_on_the_internal_app_is_a_404_without_the_token(runner):
    for path in ("/", "/health", "/internal", "/internal/observerRunner"):
        assert runner.get(path).status_code == 404


# --- The token's spelling and length -------------------------------------------------


def test_a_token_with_a_trailing_newline_still_works(runner, monkeypatch):
    """A k8s Secret mounted from a file ends in a newline."""
    monkeypatch.setattr(settings, "observer_runner_token", f"{TOKEN}\n")
    monkeypatch.setattr(
        "app.services.observer_runner_service.select_owner_assistant_pairs_on_db",
        lambda *_, **__: _async([]),
    )
    for headers in (AUTH, {"Authorization": f"Bearer  {TOKEN} "}):
        r = runner.get("/internal/observerRunner/assistants", headers=headers)
        assert r.status_code == 200, r.text


@pytest.mark.parametrize("token", ["x" * 31, " " + "x" * 31 + "\n"])
def test_a_short_token_enables_nothing(runner, monkeypatch, token):
    monkeypatch.setattr(settings, "observer_runner_token", token)
    for method, path, body in ROUTES:
        r = runner.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 404


def test_the_internal_api_doesnt_start_without_a_usable_token(monkeypatch, app_logs):
    from app import internal_api

    for token, logged in (
        ("", "internal API off: OBSERVER_RUNNER_TOKEN is not set"),
        (
            "x" * 31,
            "internal API not started: OBSERVER_RUNNER_TOKEN is shorter than 32",
        ),
    ):
        monkeypatch.setattr(settings, "observer_runner_token", token)
        assert asyncio.run(internal_api.start_internal_api()) is None
        assert any(logged in line for line in app_logs), app_logs
    assert not any("x" * 31 in line for line in app_logs)  # The token isn't logged.


def test_the_internal_api_never_takes_the_public_port(monkeypatch, app_logs):
    from app import internal_api

    monkeypatch.setattr(settings, "observer_runner_token", TOKEN)
    monkeypatch.setattr(settings, "observer_runner_port", internal_api.PUBLIC_API_PORT)
    assert asyncio.run(internal_api.start_internal_api()) is None
    assert any("the public API's port" in line for line in app_logs)


def test_the_internal_api_serves_on_its_own_port(runner, monkeypatch):
    """Started as the lifespan starts it, on a port of its own: the token
    opens it, and the public app's routes aren't there."""
    import socket

    import httpx

    from app import internal_api

    monkeypatch.setattr(
        "app.services.observer_runner_service.select_owner_assistant_pairs_on_db",
        lambda *_, **__: _async([("a" * 64, "b" * 64)]),
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def scenario() -> list[int]:
        running = await internal_api.start_internal_api(host="127.0.0.1", port=port)
        assert running is not None
        try:
            for _ in range(100):
                if running.server.started:
                    break
                await asyncio.sleep(0.02)
            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
                return [
                    (await http.get(path, headers=headers)).status_code
                    for path, headers in (
                        ("/internal/observerRunner/assistants", AUTH),
                        ("/internal/observerRunner/assistants", {}),
                        ("/health", AUTH),
                    )
                ]
        finally:
            await internal_api.stop_internal_api(running)
            assert running.task.done()

    assert asyncio.run(scenario()) == [200, 404, 404]


async def _async(value):
    return value


# --- assistants -----------------------------------------------------------------


def test_assistants_pages_by_owner(runner, monkeypatch):
    pairs = [(f"{i:064x}", f"{i + 100:064x}") for i in range(5)]
    asked: list[tuple] = []

    async def _pairs(_db, after, limit):
        asked.append((after, limit))
        rest = [p for p in pairs if after is None or p[0] > after]
        return rest[:limit]

    monkeypatch.setattr(
        "app.services.observer_runner_service.select_owner_assistant_pairs_on_db",
        _pairs,
    )

    first = runner.get("/internal/observerRunner/assistants?limit=2", headers=AUTH)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["items"] == [{"owner": o, "assistant": s} for o, s in pairs[:2]]
    assert data["next"] == pairs[1][0]

    last = runner.get(
        f"/internal/observerRunner/assistants?after={pairs[3][0]}&limit=2", headers=AUTH
    ).json()["data"]
    assert last == {
        "items": [{"owner": pairs[4][0], "assistant": pairs[4][1]}],
        "next": None,
    }
    assert asked == [(None, 3), (pairs[3][0], 3)]


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=501", "after=xyz", f"after={'A' * 64}"]
)
def test_assistants_refuses_a_bad_page(runner, query):
    r = runner.get(f"/internal/observerRunner/assistants?{query}", headers=AUTH)
    assert r.status_code == 422


# --- decrypt ----------------------------------------------------------------------


def _plaintext(response) -> str:
    assert response.status_code == 200, response.text
    return response.json()["data"]["plaintext"]


def test_decrypt_the_owners_schedule(runner, a):
    assert _plaintext(_decrypt(runner, a.pubkey, _schedule(a))) == SCHEDULE


def test_decrypt_the_assistants_own_ai_keys(runner, a):
    r = _decrypt(runner, a.pubkey, _ai_keys(a), schedule=_schedule(a))
    assert _plaintext(r) == AI_KEYS


def test_a_schedule_sent_beside_a_schedule_is_ignored(runner, a, b):
    r = _decrypt(runner, a.pubkey, _schedule(a), schedule=_schedule(b))
    assert _plaintext(r) == SCHEDULE


@pytest.mark.parametrize(
    "make",
    [
        lambda a, b: None,
        lambda a, b: {},
        # Another owner's schedule, even one naming A's Assistant.
        lambda a, b: _schedule(b),
        lambda a, b: _event(
            b.keys,
            34817,
            nip44.encrypt(b.keys, a.assistant_pubkey, SCHEDULE),
            [["d", "observer"], ["p", a.assistant_pubkey]],
        ),
        # A's schedule naming another runner, no runner, two.
        lambda a, b: _schedule(a, runner_pubkey=b.assistant_pubkey),
        lambda a, b: _schedule(a, tags=[["d", "observer"]]),
        lambda a, b: _schedule(
            a, tags=[["p", a.assistant_pubkey], ["p", b.assistant_pubkey]]
        ),
        # Not a verified 34817.
        lambda a, b: {**_schedule(a), "sig": _schedule(b)["sig"]},
        lambda a, b: {**_schedule(a), "created_at": _schedule(a)["created_at"] + 1},
        lambda a, b: _event(
            a.keys,
            34818,
            nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE),
            [["p", a.assistant_pubkey]],
        ),
        lambda a, b: _ai_keys(a),
    ],
    ids=[
        "no-schedule",
        "empty",
        "other-owners",
        "other-owner-naming-a",
        "other-runner",
        "no-runner",
        "two-runners",
        "bad-sig",
        "tampered",
        "kind-34818",
        "the-12000-itself",
    ],
)
def test_the_ai_keys_open_only_beside_the_owners_schedule_naming_the_assistant(
    runner, a, b, app_logs, make
):
    r = _decrypt(runner, a.pubkey, _ai_keys(a), schedule=make(a, b))
    assert r.status_code == 403, r.text
    assert "sk-or-v1-very-secret" not in r.text
    assert any(
        f"owner={a.pubkey} kind=12000 outcome=refused" in line for line in app_logs
    )


def _resigned(event: dict, signer: Keys) -> dict:
    """`event` with its fields as given, re-signed by `signer`."""
    return _event(signer, event["kind"], event["content"], event["tags"])


@pytest.mark.parametrize(
    "make",
    [
        # Another kind, even one sealed between the same two keys.
        lambda a, b: _event(
            a.keys,
            4,
            nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE),
            [["p", a.assistant_pubkey]],
        ),
        lambda a, b: _event(
            a.keys,
            14,
            nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE),
            [["p", a.assistant_pubkey]],
        ),
        lambda a, b: _event(
            a.keys,
            34818,
            nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE),
            [["p", a.assistant_pubkey]],
        ),
        lambda a, b: {**_schedule(a), "kind": "34817"},
        lambda a, b: {**_schedule(a), "kind": 34817.0},
        # A bad signature, a bad id, a missing signature.
        lambda a, b: {**_schedule(a), "sig": _schedule(b)["sig"]},
        lambda a, b: {**_schedule(a), "id": "0" * 64},
        lambda a, b: {**_schedule(a), "created_at": _schedule(a)["created_at"] + 1},
        lambda a, b: {k: v for k, v in _schedule(a).items() if k != "sig"},
        # A schedule by another author: a stranger, A's Assistant, B.
        lambda a, b: _resigned(_schedule(a), Keys.generate()),
        lambda a, b: _resigned(_schedule(a), a.assistant),
        lambda a, b: _resigned(_schedule(a), b.keys),
        # No `p`, two `p`, a `p` naming another Assistant or the owner.
        lambda a, b: _schedule(a, tags=[["d", "observer"]]),
        lambda a, b: _schedule(
            a, tags=[["p", a.assistant_pubkey], ["p", a.assistant_pubkey]]
        ),
        lambda a, b: _schedule(
            a, tags=[["p", a.assistant_pubkey], ["p", b.assistant_pubkey]]
        ),
        lambda a, b: _schedule(a, runner_pubkey=b.assistant_pubkey),
        lambda a, b: _schedule(a, runner_pubkey=a.pubkey),
        lambda a, b: _schedule(a, tags=[["p"]]),
        # A 12000 by another Assistant, by the owner, by a stranger.
        lambda a, b: _ai_keys(b),
        lambda a, b: _resigned(_ai_keys(a), a.keys),
        lambda a, b: _resigned(_ai_keys(a), Keys.generate()),
        # Not an event at all.
        lambda a, b: {},
        lambda a, b: {"kind": 34817, "content": _schedule(a)["content"]},
    ],
    ids=[
        "kind-4",
        "kind-14",
        "kind-34818",
        "kind-as-string",
        "kind-as-float",
        "bad-sig",
        "bad-id",
        "tampered-created_at",
        "no-sig",
        "stranger-author",
        "assistant-author",
        "other-owner-author",
        "no-p",
        "two-p-same",
        "two-p",
        "p-other-assistant",
        "p-owner",
        "empty-p",
        "12000-other-assistant",
        "12000-by-owner",
        "12000-by-stranger",
        "empty",
        "unsigned",
    ],
)
def test_decrypt_refuses_anything_but_the_two_events(runner, a, b, app_logs, make):
    event = make(a, b)
    # A valid schedule beside it, so a 12000 is refused for what it is.
    r = _decrypt(runner, a.pubkey, event, schedule=_schedule(a))
    assert r.status_code == 403, r.text
    assert "plaintext" not in r.text and SCHEDULE not in r.text
    assert "sk-or-v1-very-secret" not in r.text
    assert any(f"owner={a.pubkey}" in line and "refused" in line for line in app_logs)


# --- What counts as a `p`, and a schedule that no longer asks for a print ----------


def test_a_p_without_a_value_isnt_a_p(runner, a):
    """`["p"]` names nobody (NIP-FB): beside the Assistant's `p`, the schedule
    still names exactly the Assistant, as Quartz reads it."""
    schedule = _schedule(a, tags=[["d", "observer"], ["p"], ["p", a.assistant_pubkey]])
    assert _plaintext(_decrypt(runner, a.pubkey, schedule)) == SCHEDULE
    r = _decrypt(runner, a.pubkey, _ai_keys(a), schedule=schedule)
    assert _plaintext(r) == AI_KEYS


@pytest.mark.parametrize(
    "tags",
    [
        [["d", "observer"], ["p", ""]],
        [["d", "observer"], ["p", ""], ["p", "{assistant}"]],
        [["d", "observer"], ["p", "{assistant}"], ["p", ""]],
        # Not the lowercase key: no runner, as Quartz reads it.
        [["d", "observer"], ["p", "{ASSISTANT}"]],
    ],
    ids=["blank", "blank-then-assistant", "assistant-then-blank", "uppercase"],
)
def test_a_blank_p_is_a_p_that_isnt_the_assistant(runner, a, app_logs, tags):
    """`["p", ""]` counts (NIP-FB): it stops the owner's devices and nobody runs
    the task, so the Assistant opens neither the schedule nor its keys."""
    tags = [
        [
            v.format(assistant=a.assistant_pubkey, ASSISTANT=a.assistant_pubkey.upper())
            for v in t
        ]
        for t in tags
    ]
    schedule = _schedule(a, tags=tags)
    assert [t for t in schedule["tags"] if t[0] == "p"] == [
        t for t in tags if t[0] == "p"
    ]
    assert _decrypt(runner, a.pubkey, schedule).status_code == 403
    r = _decrypt(runner, a.pubkey, _ai_keys(a), schedule=schedule)
    assert r.status_code == 403
    assert "sk-or-v1-very-secret" not in r.text


def _schedule_with(owner: "Owner", private: str, tags=None) -> dict:
    return _event(
        owner.keys,
        34817,
        nip44.encrypt(owner.keys, owner.assistant_pubkey, private),
        tags
        if tags is not None
        else [["d", "observer"], ["p", owner.assistant_pubkey]],
    )


@pytest.mark.parametrize(
    "make",
    [
        lambda a: _schedule(
            a, tags=[["d", "observer"], ["p", a.assistant_pubkey], ["paused"]]
        ),
        lambda a: _schedule_with(a, '[["task","observer"],["paused"]]'),
    ],
    ids=["public-paused", "private-paused"],
)
def test_a_paused_schedule_opens_no_ai_keys(runner, a, app_logs, make):
    """Paused, the owner asked for no print: the keys stay shut. The schedule
    itself still opens, so the worker can see it's paused."""
    schedule = make(a)
    assert _decrypt(runner, a.pubkey, schedule).status_code == 200
    r = _decrypt(runner, a.pubkey, _ai_keys(a), schedule=schedule)
    assert r.status_code == 403, r.text
    assert "sk-or-v1-very-secret" not in r.text
    assert any(
        f"owner={a.pubkey} kind=12000 outcome=refused (schedule paused)" in line
        for line in app_logs
    )


def test_a_schedule_the_assistant_cant_read_opens_no_ai_keys(runner, a):
    """Sealed to someone else: the runner can't read it, so it can't run it."""
    stranger = Keys.generate()
    schedule = _event(
        a.keys,
        34817,
        nip44.encrypt(a.keys, stranger.public_key().to_hex(), SCHEDULE),
        [["d", "observer"], ["p", a.assistant_pubkey]],
    )
    r = _decrypt(runner, a.pubkey, _ai_keys(a), schedule=schedule)
    assert r.status_code == 403
    assert "sk-or-v1-very-secret" not in r.text


def test_a_schedule_with_no_private_tags_opens_the_ai_keys(runner, a):
    schedule = _event(a.keys, 34817, "", [["d", "observer"], ["p", a.assistant_pubkey]])
    assert (
        _plaintext(_decrypt(runner, a.pubkey, _ai_keys(a), schedule=schedule))
        == AI_KEYS
    )


def test_a_owner_with_bs_schedule_is_refused(runner, a, b):
    """B's schedule, naming B's Assistant, sent as A's: refused, and A's
    Assistant decrypts nothing. Sent as B's, it opens with B's Assistant."""
    schedule = _schedule(b)
    assert _decrypt(runner, a.pubkey, schedule).status_code == 403
    assert _decrypt(runner, a.pubkey, _ai_keys(b)).status_code == 403
    assert _plaintext(_decrypt(runner, b.pubkey, schedule)) == SCHEDULE


def test_b_schedule_naming_as_assistant_is_refused_for_either_owner(runner, a, b):
    """Anyone can tag any Assistant: B naming A's Assistant prints nothing."""
    schedule = _event(
        b.keys,
        34817,
        nip44.encrypt(b.keys, a.assistant_pubkey, SCHEDULE),
        [["d", "observer"], ["p", a.assistant_pubkey]],
    )
    assert _decrypt(runner, a.pubkey, schedule).status_code == 403
    assert _decrypt(runner, b.pubkey, schedule).status_code == 403


def test_a_valid_event_whose_content_isnt_for_the_pair_is_a_422(runner, a):
    """Signed by A and naming A's Assistant, but sealed to someone else."""
    stranger = Keys.generate()
    schedule = _event(
        a.keys,
        34817,
        nip44.encrypt(a.keys, stranger.public_key().to_hex(), SCHEDULE),
        [["d", "observer"], ["p", a.assistant_pubkey]],
    )
    r = _decrypt(runner, a.pubkey, schedule)
    assert r.status_code == 422
    assert SCHEDULE not in r.text


def test_decrypt_takes_no_bare_ciphertext(runner, a):
    """The old `{peer, ciphertext}` body is gone: no oracle for the pair."""
    ciphertext = nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE)
    r = runner.post(
        "/internal/observerRunner/decrypt",
        json={"owner": a.pubkey, "peer": "owner", "ciphertext": ciphertext},
        headers=AUTH,
    )
    assert r.status_code == 422
    assert SCHEDULE not in r.text


def test_decrypt_for_an_owner_with_no_assistant_is_a_404(runner):
    stranger = Owner()
    r = _decrypt(runner, stranger.pubkey, _schedule(stranger))
    assert r.status_code == 404
    assert r.headers["X-Reason"] == "no-assistant"


def test_decrypt_logs_no_plaintext_or_ciphertext(runner, a, b, app_logs):
    schedule, ai_keys = _schedule(a), _ai_keys(a)
    _decrypt(runner, a.pubkey, schedule)
    _decrypt(runner, a.pubkey, ai_keys, schedule=schedule)
    _decrypt(runner, a.pubkey, _schedule(b))  # refused
    joined = "\n".join(app_logs)
    assert f"owner={a.pubkey} kind=34817 outcome=ok" in joined
    assert f"owner={a.pubkey} kind=12000 outcome=ok" in joined
    for secret in (SCHEDULE, AI_KEYS, "sk-or-v1-very-secret"):
        assert secret not in joined
    for ciphertext in (schedule["content"], ai_keys["content"]):
        assert ciphertext not in joined


# --- sign ---------------------------------------------------------------------------


def _signed(response) -> dict:
    assert response.status_code == 200, response.text
    event = response.json()["data"]
    assert Event.from_json(json.dumps(event)).verify()
    return event


def test_sign_an_observer_site(runner, a):
    tags = _site_tags(a.pubkey)
    event = _signed(_sign(runner, a.pubkey, 35128, tags, content=""))
    assert event["pubkey"] == a.assistant_pubkey
    assert event["kind"] == 35128
    assert event["tags"] == [*tags, ["client", "Brainstorm"]]


def test_sign_a_blossom_upload_authorization(runner, a):
    now = int(time.time())
    tags = _upload_tags(now + 300)
    event = _signed(_sign(runner, a.pubkey, 24242, tags, content="Upload edition"))
    assert event["pubkey"] == a.assistant_pubkey
    assert event["tags"][:3] == tags
    assert event["content"] == "Upload edition"


def test_the_workers_client_tag_is_replaced_by_ours(runner, a):
    tags = [*_site_tags(a.pubkey), ["client", "Somebody else"]]
    event = _signed(_sign(runner, a.pubkey, 35128, tags))
    assert [t for t in event["tags"] if t[0] == "client"] == [["client", "Brainstorm"]]


def test_a_sign_for_one_owner_never_produces_anothers_signature(runner, a, b):
    for owner in (a, b):
        event = _signed(_sign(runner, owner.pubkey, 35128, _site_tags(owner.pubkey)))
        assert event["pubkey"] == owner.assistant_pubkey
    # A's owner, B's site: refused, not signed by either.
    assert _sign(runner, a.pubkey, 35128, _site_tags(b.pubkey)).status_code == 403


@pytest.mark.parametrize(
    "kind", [0, 1, 3, 5, 10002, 10040, 12000, 30382, 34817, 39999, 22242, 27235, 35129]
)
def test_sign_refuses_every_other_kind(runner, a, kind):
    r = _sign(runner, a.pubkey, kind, _site_tags(a.pubkey))
    assert r.status_code == 403


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o, t: [x for x in t if x[0] != "d"],
        lambda o, t: [["d", "observer-2"] if x[0] == "d" else x for x in t],
        lambda o, t: [*t, ["d", "observer"]],
        lambda o, t: [x for x in t if x[0] != "p"],
        lambda o, t: [["p", "0" * 64] if x[0] == "p" else x for x in t],
        lambda o, t: [*t, ["p", "0" * 64]],
        lambda o, t: [["p", o.upper()] if x[0] == "p" else x for x in t],
    ],
    ids=["no-d", "other-d", "two-d", "no-p", "other-p", "two-p", "uppercase-p"],
)
def test_sign_refuses_a_site_that_isnt_the_owners_observer(runner, a, mutate):
    r = _sign(runner, a.pubkey, 35128, mutate(a.pubkey, _site_tags(a.pubkey)))
    assert r.status_code == 403


@pytest.mark.parametrize(
    "tags",
    [
        lambda now: [["t", "delete"], ["expiration", str(now + 60)]],
        lambda now: [["t", "list"], ["expiration", str(now + 60)]],
        lambda now: [["t", "upload"], ["t", "delete"], ["expiration", str(now + 60)]],
        lambda now: [["expiration", str(now + 60)]],
        lambda now: [["t", "upload"]],
        lambda now: [["t", "upload"], ["expiration", str(now + 601)]],
        lambda now: [["t", "upload"], ["expiration", str(now + 86400)]],
        lambda now: [["t", "upload"], ["expiration", str(now - 1)]],
        lambda now: [["t", "upload"], ["expiration", "soon"]],
        lambda now: [["t", "upload"], ["expiration", "\u00b2"]],
        lambda now: [["t", "upload"], ["expiration", "\u00b9" + str(now + 60)[1:]]],
        lambda now: [
            ["t", "upload"],
            ["expiration", "".join(chr(0x0660 + int(c)) for c in str(now + 60))],
        ],
        lambda now: [["t", "upload"], ["expiration", f" {now + 60}"]],
        lambda now: [["t", "upload"], ["expiration", f"+{now + 60}"]],
        lambda now: [["t", "upload"], ["expiration", "1" * 11]],
        lambda now: [
            ["t", "upload"],
            ["expiration", str(now + 60)],
            ["expiration", str(now + 60)],
        ],
    ],
    ids=[
        "delete",
        "list",
        "upload+delete",
        "no-t",
        "no-expiration",
        "601s",
        "a-day",
        "expired",
        "not-a-number",
        "superscript-two",
        "superscript-digit",
        "arabic-indic-digits",
        "leading-space",
        "plus-sign",
        "eleven-digits",
        "two-expirations",
    ],
)
def test_sign_refuses_any_blossom_auth_but_a_short_upload(runner, a, tags):
    r = _sign(runner, a.pubkey, 24242, tags(int(time.time())))
    assert r.status_code == 403


def test_sign_refuses_a_blossom_auth_naming_the_assistant(runner, a, app_logs):
    """nostr-sdk would drop the `p` naming the signer, and the event would then
    fail its own check as a 500. It's refused, a 403, before anything is signed."""
    tags = [*_upload_tags(int(time.time()) + 300), ["p", a.assistant_pubkey]]
    r = _sign(runner, a.pubkey, 24242, tags)
    assert r.status_code == 403
    assert "p tag" in r.json()["detail"]
    assert not any("failed its own check" in line for line in app_logs)
    # In capitals too: nostr-sdk reads it as the same key.
    tags = [*_upload_tags(int(time.time()) + 300), ["p", a.assistant_pubkey.upper()]]
    r = _sign(runner, a.pubkey, 24242, tags)
    assert r.status_code == 403
    assert "p tag" in r.json()["detail"]
    assert not any("failed its own check" in line for line in app_logs)
    # A `p` naming someone else is the worker's business.
    tags = [*_upload_tags(int(time.time()) + 300), ["p", a.pubkey]]
    assert _signed(_sign(runner, a.pubkey, 24242, tags))["tags"][-2] == ["p", a.pubkey]


def test_sign_refuses_an_event_dated_far_ahead(runner, a):
    r = _sign(
        runner,
        a.pubkey,
        35128,
        _site_tags(a.pubkey),
        created_at=int(time.time()) + 3600,
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "35128", "created_at": 1, "tags": [], "content": ""},
        {"kind": 35128, "created_at": 1, "tags": [["d", 1]], "content": ""},
        {"kind": 35128, "created_at": 1, "tags": [[]], "content": ""},
        {"kind": 35128, "created_at": 1, "tags": []},
    ],
)
def test_sign_refuses_a_malformed_event(runner, a, event):
    r = runner.post(
        "/internal/observerRunner/sign",
        json={"owner": a.pubkey, "event": event},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_sign_for_an_owner_with_no_assistant_is_a_404(runner):
    owner = "e" * 64
    r = _sign(runner, owner, 35128, _site_tags(owner))
    assert r.status_code == 404


def test_the_assistants_query_skips_unfilled_rows_and_pages_by_owner():
    """The repo's statement against the real model, compiled for Postgres."""
    from sqlalchemy.dialects import postgresql

    from app.repos.brainstorm_nsec import select_owner_assistant_pairs_on_db

    captured: list = []

    class _Db:
        async def execute(self, statement):
            captured.append(statement)
            return SimpleNamespace(all=lambda: [("o" * 64, "s" * 64)])

    rows = asyncio.run(
        select_owner_assistant_pairs_on_db(_Db(), after="a" * 64, limit=3)
    )

    assert rows == [("o" * 64, "s" * 64)]
    sql = str(
        captured[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "brainstorm_nsec.assistant_pubkey IS NOT NULL" in sql
    assert f"brainstorm_nsec.pubkey > '{'a' * 64}'" in sql
    assert "ORDER BY brainstorm_nsec.pubkey" in sql and "LIMIT 3" in sql
    assert "nsec," not in sql.split("FROM")[0]  # No secret column is read.
