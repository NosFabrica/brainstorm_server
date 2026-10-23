"""Session tokens: standard JWT claims, library-enforced expiry."""

import asyncio
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import get_db
from app.core.sql_admin_panel import AdminAuth
from app.services.auth_service import generate_authentication_token
from app.utils.api_validators import verify_token
from app.utils.auth.auth_util import sql_admin_create_jwt_token

PUBKEY = "a" * 64


def _sign(payload: dict, key: str | None = None, algorithm: str | None = None) -> str:
    return jwt.encode(
        payload,
        key or settings.auth_secret_key,
        algorithm=algorithm or settings.auth_algorithm,
    )


@pytest.fixture
def guarded(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "app.utils.api_validators.read_session_version_on_db", AsyncMock(return_value=0)
    )
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(jwt_data=Depends(verify_token)):
        return {"pubkey": jwt_data.nostr_pubkey, "is_admin": jwt_data.is_admin}

    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _call(client: TestClient, token: str):
    return client.get("/whoami", headers={"Authorization": f"Bearer {token}"})


def test_new_token_carries_standard_claims():
    before = int(time.time())
    token = generate_authentication_token(PUBKEY).token

    claims = jwt.decode(
        token, settings.auth_secret_key, algorithms=[settings.auth_algorithm]
    )

    assert claims["sub"] == PUBKEY
    assert before <= claims["iat"] <= int(time.time())
    ttl = settings.auth_access_token_expire_minutes * 60
    assert claims["exp"] - claims["iat"] == ttl
    assert claims["is_admin"] is False


def test_fresh_token_authenticates_its_subject(guarded):
    token = generate_authentication_token(PUBKEY).token

    r = _call(guarded, token)

    assert r.status_code == 200
    assert r.json() == {"pubkey": PUBKEY, "is_admin": False}


def _claims(**over) -> dict:
    now = int(time.time())
    return {"sub": PUBKEY, "iat": now, "exp": now + 3600, "is_admin": False} | over


def test_expired_token_is_401_not_500(guarded):
    now = int(time.time())
    token = _sign(_claims(iat=now - 7200, exp=now - 3600))

    assert _call(guarded, token).status_code == 401


def test_expiry_within_leeway_still_authenticates(guarded):
    token = _sign(_claims(exp=int(time.time()) - 5))

    assert _call(guarded, token).status_code == 200


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_sign(_claims(), key="not-the-secret"), id="bad-signature"),
        pytest.param("not.a.jwt", id="malformed"),
        pytest.param(_sign(_claims(), algorithm="HS512"), id="wrong-algorithm"),
        pytest.param(jwt.encode(_claims(), None, algorithm="none"), id="alg-none"),
        pytest.param(
            _sign({k: v for k, v in _claims().items() if k != "sub"}), id="no-sub"
        ),
        pytest.param(
            _sign({k: v for k, v in _claims().items() if k != "iat"}), id="no-iat"
        ),
        pytest.param(_sign({"sub": PUBKEY, "iat": int(time.time())}), id="no-exp"),
        pytest.param(_sign(_claims(sub=42)), id="non-string-sub"),
    ],
)
def test_invalid_token_is_401(guarded, token):
    assert _call(guarded, token).status_code == 401


def test_legacy_expires_date_token_is_401(guarded):
    token = _sign(
        {"nostr_pubkey": PUBKEY, "token_type": "USER", "expires_date": "2999-01-01T00:00:00"}
    )

    assert _call(guarded, token).status_code == 401


class _AdminPanelRequest:
    def __init__(self, token: str | None) -> None:
        self.session = {"admin_panel_token": token} if token else {}

    def url_for(self, name: str) -> str:
        return "http://testserver/admin/login"


def _admin_panel_allows(token: str | None) -> bool:
    result = asyncio.run(
        AdminAuth(secret_key="x").authenticate(_AdminPanelRequest(token))
    )
    return result is True


def test_admin_panel_token_uses_standard_exp():
    token = sql_admin_create_jwt_token()
    claims = jwt.decode(
        token, settings.auth_secret_key, algorithms=[settings.auth_algorithm]
    )

    assert claims["exp"] - claims["iat"] == 15 * 60
    assert _admin_panel_allows(token)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(None, id="missing"),
        pytest.param(_sign({"iat": 0, "exp": 1}), id="expired"),
        pytest.param(_sign({"expires_date": "2999-01-01T00:00:00"}), id="legacy-shape"),
        pytest.param(
            _sign({"iat": 0, "exp": 9999999999}, key="nope"), id="bad-signature"
        ),
        pytest.param("garbage", id="malformed"),
    ],
)
def test_admin_panel_rejects_invalid_token(token):
    assert not _admin_panel_allows(token)
