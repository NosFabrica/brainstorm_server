"""Session revocation: a per-user session version that old tokens fall behind."""

import asyncio
import base64
from datetime import timedelta
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Tag

from app.core.config import settings
from app.core.database import get_db
from app.repos import session_version_repo
from app.routers.router import router
from app.services import session_service
from app.utils.api_validators import verify_token
from app.utils.auth.auth_util import create_jwt_token
from tests.test_auth_challenge import _challenge
from tests.test_auth_challenge import _FakeRedis as _ChallengeRedis
from tests.test_auth_challenge import _signed, _verify

USER = "a" * 64


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, ex: int | None = None):
        self.store[key] = str(value)
        return True


class _FakeVersions:
    """The users table, as far as session versions go."""

    def __init__(self) -> None:
        self.versions: dict[str, int] = {}
        self.reads = 0

    async def get(self, db, pubkey: str) -> int:
        self.reads += 1
        return self.versions.get(pubkey, 0)

    async def bump(self, db, pubkey: str) -> int | None:
        if pubkey not in self.versions:
            return None
        self.versions[pubkey] += 1
        return self.versions[pubkey]


@pytest.fixture
def versions(monkeypatch) -> _FakeVersions:
    fake = _FakeVersions()
    fake.versions[USER] = 0
    monkeypatch.setattr(session_version_repo, "get_session_version_on_db", fake.get)
    monkeypatch.setattr(session_service, "bump_session_version_on_db", fake.bump)
    monkeypatch.setattr(session_version_repo, "redis_client", _FakeRedis())
    return fake


@pytest.fixture
def api(versions) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    @app.get("/whoami")
    async def whoami(jwt_data=Depends(verify_token)):
        return {"pubkey": jwt_data.nostr_pubkey}

    async def _fake_get_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _fake_get_db
    return TestClient(app, raise_server_exceptions=False)


def _token(pubkey: str = USER, session_version: int | None = 0) -> str:
    return create_jwt_token(
        pubkey, ttl=timedelta(minutes=60), session_version=session_version
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _whoami(api: TestClient, token: str) -> int:
    return api.get("/whoami", headers=_auth(token)).status_code


def test_token_behind_the_users_version_is_401(api, versions):
    versions.versions[USER] = 2

    assert _whoami(api, _token(session_version=1)) == 401


def test_token_at_the_users_version_is_ok(api, versions):
    versions.versions[USER] = 2

    assert _whoami(api, _token(session_version=2)) == 200


def _unversioned_token() -> str:
    """Minted before session versions existed: no `session_version` claim."""
    claims = jwt.decode(
        _token(), settings.auth_secret_key, algorithms=[settings.auth_algorithm]
    )
    del claims["session_version"]
    return jwt.encode(claims, settings.auth_secret_key, settings.auth_algorithm)


def test_unversioned_token_counts_as_version_zero(api, versions):
    assert _whoami(api, _unversioned_token()) == 200

    asyncio.run(session_service.revoke_sessions(AsyncMock(), USER))

    assert _whoami(api, _unversioned_token()) == 401


def test_version_check_is_cached_not_read_per_request(api, versions):
    token = _token()

    for _ in range(3):
        assert _whoami(api, token) == 200

    assert versions.reads == 1


def test_sign_out_everywhere_revokes_the_callers_own_tokens(api):
    token = _token()

    assert api.post("/user/sessions/revoke", headers=_auth(token)).status_code == 204

    assert _whoami(api, token) == 401


@pytest.fixture
def login(api, versions, monkeypatch):
    """Run the real challenge → signed event → token exchange."""
    from app.services import auth_service

    monkeypatch.setattr(auth_service, "redis_client", _ChallengeRedis())
    monkeypatch.setattr(
        auth_service,
        "brainstorm_nsec_exists_by_pubkey_on_db",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        auth_service,
        "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db",
        AsyncMock(),
    )

    def _login(keys: Keys) -> str:
        pubkey = keys.public_key().to_hex()
        r = _verify(api, pubkey, _signed(keys, _challenge(api, pubkey)))
        assert r.status_code == 200, r.text
        return r.json()["data"]["token"]

    return _login


def test_signing_in_again_after_revoking_gets_a_working_token(api, versions, login):
    keys = Keys.generate()
    versions.versions[keys.public_key().to_hex()] = 0
    old = login(keys)
    assert api.post("/user/sessions/revoke", headers=_auth(old)).status_code == 204

    new = login(keys)

    assert _whoami(api, new) == 200
    assert _whoami(api, old) == 401


ADMIN = "b" * 64


@pytest.fixture
def admins(monkeypatch, versions):
    from app.core.admin_whitelist import init_admin_whitelist

    monkeypatch.setattr(settings, "admin_enabled", True)
    monkeypatch.setattr(settings, "admin_whitelisted_pubkeys", ADMIN)
    init_admin_whitelist()
    versions.versions[ADMIN] = 0
    yield
    monkeypatch.undo()
    init_admin_whitelist()


def test_admin_revokes_a_users_sessions(api, admins):
    users_token = _token(USER)

    r = api.post(f"/admin/users/{USER}/sessions/revoke", headers=_auth(_token(ADMIN)))

    assert r.status_code == 204
    assert _whoami(api, users_token) == 401


def test_non_admin_cannot_revoke_someone_elses_sessions(api, admins, versions):
    versions.versions["c" * 64] = 0
    victims_token = _token("c" * 64)

    r = api.post(
        f"/admin/users/{'c' * 64}/sessions/revoke", headers=_auth(_token(USER))
    )

    assert r.status_code == 403
    assert _whoami(api, victims_token) == 200


def test_admin_revoke_of_an_unknown_user_is_404(api, admins):
    r = api.post(
        f"/admin/users/{'d' * 64}/sessions/revoke", headers=_auth(_token(ADMIN))
    )

    assert r.status_code == 404


def _nip98(keys: Keys, method: str, path: str) -> dict:
    tags = [
        Tag.parse(["u", f"{settings.public_base_url.rstrip('/')}{path}"]),
        Tag.parse(["method", method]),
    ]
    event = EventBuilder(Kind(27235), "").tags(tags).sign_with_keys(keys)
    encoded = base64.b64encode(event.as_json().encode()).decode()
    return {"Authorization": f"Nostr {encoded}"}


def test_nip98_requests_are_unaffected_by_revocation(api, versions, monkeypatch):
    from app.utils import api_validators

    monkeypatch.setattr(
        api_validators,
        "brainstorm_nsec_exists_by_pubkey_on_db",
        AsyncMock(return_value=True),
    )
    keys = Keys.generate()
    versions.versions[keys.public_key().to_hex()] = 5

    r = api.get("/whoami", headers=_nip98(keys, "GET", "/whoami"))

    assert r.status_code == 200
