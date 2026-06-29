"""Shared fixtures for the fast (mocked) test suite.

The FastAPI app is imported and driven via ``TestClient`` *without* entering it
as a context manager, so the lifespan (Neo4j connectivity check + background
consumers) never runs and no real services are needed. Auth is satisfied by
overriding the ``verify_token`` dependency to inject a caller pubkey, and the
Neo4j/Redis side-effects are mocked per-test.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make ``Settings()`` constructible before importing the app. We first load the
# real ``.env`` (so integration tests reach the actual local stack), then fill
# any *still-missing* required settings with dummies so the fast suite — which
# is driven without the app lifespan and mocks every Neo4j/Redis call — imports
# regardless of ``.env`` drift. ``setdefault`` means a value already in the
# environment or in ``.env`` always wins over the dummy.
_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_DUMMY_ENV = {
    "DB_URL": "postgresql+asyncpg://u:p@localhost:5432/test",
    "DEPLOY_ENVIRONMENT": "TEST",
    "AUTH_ALGORITHM": "HS256",
    "AUTH_SECRET_KEY": "test-secret",
    "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES": "60",
    "SQL_ADMIN_USERNAME": "admin",
    "SQL_ADMIN_PASSWORD": "admin",
    "SQL_ADMIN_SECRET_KEY": "admin-secret",
    "NEO4J_DB_URL": "bolt://localhost:7687",
    "NEO4J_DB_USERNAME": "neo4j",
    "NEO4J_DB_PASSWORD": "neo4j",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "NOSTR_TRANSFER_FROM_RELAY": "ws://localhost:7777",
    "NOSTR_TRANSFER_TO_RELAY": "ws://localhost:7777",
    "NOSTR_UPLOAD_TA_EVENTS_RELAY": "ws://localhost:7777",
    "NOSTR_UPLOAD_TA_EVENTS_RELAY_PUBLIC_URL": "ws://localhost:7777",
    "CUTOFF_OF_VALID_GRAPERANK_SCORES": "0.05",
    "PERFORM_NOSTR_FULL_SYNC": "false",
    "FRONTEND_URL": "http://localhost:3000",
    "PUBLIC_BASE_URL": "http://localhost:8080",
    "VESPA_URL": "http://localhost:8080",
}
for _key, _value in _DUMMY_ENV.items():
    os.environ.setdefault(_key, _value)

from fastapi import Request
from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Tag

from app.api import app
from app.utils.api_validators import verify_token
from app.utils.auth.auth_models import JWTData


def signed_event(
    keys: Keys, kind: int, follow_pubkeys: list[str] | None = None
) -> dict:
    """Build and sign a Nostr event, returned as the plain dict the API accepts."""
    tags = [Tag.parse(["p", pk]) for pk in (follow_pubkeys or [])]
    event = EventBuilder(Kind(kind), "").tags(tags).sign_with_keys(keys)
    return json.loads(event.as_json())


def signed_kind3(keys: Keys, follow_pubkeys: list[str]) -> dict:
    return signed_event(keys, 3, follow_pubkeys)


class _Caller:
    """The authenticated caller; holds the keypair whose pubkey the JWT carries."""

    def __init__(self) -> None:
        self.keys = Keys.generate()

    @property
    def pubkey(self) -> str:
        return self.keys.public_key().to_hex()


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def client(caller: _Caller):
    async def _fake_verify_token(request: Request) -> None:
        request.state.jwt_data = JWTData(
            nostr_pubkey=caller.pubkey, expires_date=datetime.max
        )

    app.dependency_overrides[verify_token] = _fake_verify_token
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def mock_rate_limit(monkeypatch) -> AsyncMock:
    """No-op rate limiter by default; tests can set ``.side_effect`` to trip it."""
    limiter = AsyncMock()
    monkeypatch.setattr(
        "app.routers.user.router.validateIfRequestedTooOftenByIP", limiter
    )
    return limiter


@pytest.fixture
def mock_kind3_write(monkeypatch) -> AsyncMock:
    """Patch the reused kind-3 handler + Neo4j session opener.

    Returns the ``process_event_kind_3`` mock so tests can assert how it was
    called or make it raise (e.g. transient errors).
    """
    process_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.onboarding_service.process_event_kind_3", process_mock
    )

    @asynccontextmanager
    async def _fake_session():
        yield AsyncMock()

    fake_driver = MagicMock()
    fake_driver.session = lambda: _fake_session()
    monkeypatch.setattr("app.services.onboarding_service.neo4j_driver", fake_driver)

    return process_mock
