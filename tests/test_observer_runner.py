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
from nostr_sdk import Event, Keys

from app.api import app
from app.core.config import settings
from app.core.database import get_db
from app.utils import nip44

TOKEN = "runner-token-0123456789abcdef"
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

    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _decrypt(client, owner: str, peer: str, ciphertext: str, headers=AUTH):
    return client.post(
        "/internal/observerRunner/decrypt",
        json={"owner": owner, "peer": peer, "ciphertext": ciphertext},
        headers=headers,
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
        {"owner": "0" * 64, "peer": "self", "ciphertext": "x"},
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

    monkeypatch.setattr("app.routers.observer_runner.router.hmac.compare_digest", _spy)
    runner.get(
        "/internal/observerRunner/assistants", headers={"Authorization": "Bearer nope"}
    )
    assert calls == [(b"nope", TOKEN.encode())]


def test_the_routes_are_not_in_the_public_api_document(runner):
    paths = runner.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/internal")]


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


def test_decrypt_a_schedule_from_the_owner(runner, a):
    ciphertext = nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE)
    r = _decrypt(runner, a.pubkey, "owner", ciphertext)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"plaintext": SCHEDULE}


def test_decrypt_the_assistants_own_list(runner, a):
    ciphertext = nip44.encrypt(a.assistant, a.assistant_pubkey, AI_KEYS)
    r = _decrypt(runner, a.pubkey, "self", ciphertext)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"plaintext": AI_KEYS}


@pytest.mark.parametrize("peer", ["assistant", "other", "OWNER", "", "0" * 64])
def test_decrypt_takes_no_other_peer(runner, a, peer):
    ciphertext = nip44.encrypt(a.keys, a.assistant_pubkey, SCHEDULE)
    r = _decrypt(runner, a.pubkey, peer, ciphertext)
    assert r.status_code == 422
    assert SCHEDULE not in r.text and ciphertext not in r.text


def test_decrypt_for_one_owner_never_opens_anothers(runner, a, b):
    """B's schedule (B -> B's Assistant), or one a stranger sent A's Assistant,
    named as A's: neither A's Assistant with A, nor with itself, opens it."""
    stranger = Keys.generate()
    for ciphertext in (
        nip44.encrypt(b.keys, b.assistant_pubkey, SCHEDULE),
        nip44.encrypt(b.assistant, b.assistant_pubkey, AI_KEYS),
        nip44.encrypt(stranger, a.assistant_pubkey, SCHEDULE),
    ):
        for peer in ("owner", "self"):
            r = _decrypt(runner, a.pubkey, peer, ciphertext)
            assert r.status_code == 422, r.text
            assert "plaintext" not in r.text


def test_decrypt_for_an_owner_with_no_assistant_is_a_404(runner):
    r = _decrypt(runner, "f" * 64, "owner", "AgAA")
    assert r.status_code == 404
    assert r.headers["X-Reason"] == "no-assistant"


def test_decrypt_logs_no_plaintext_or_ciphertext(runner, a, app_logs):
    ciphertext = nip44.encrypt(a.assistant, a.assistant_pubkey, AI_KEYS)
    _decrypt(runner, a.pubkey, "self", ciphertext)
    _decrypt(runner, a.pubkey, "owner", ciphertext)  # fails: wrong peer
    joined = "\n".join(app_logs)
    assert f"owner={a.pubkey} peer=self outcome=ok" in joined
    assert "sk-or-v1-very-secret" not in joined and ciphertext not in joined


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
        "two-expirations",
    ],
)
def test_sign_refuses_any_blossom_auth_but_a_short_upload(runner, a, tags):
    r = _sign(runner, a.pubkey, 24242, tags(int(time.time())))
    assert r.status_code == 403


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
