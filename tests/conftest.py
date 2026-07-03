"""Shared fixtures + settings bootstrap for the test suites.

Two suites share this tree:

- The fast, mocked suite (``tests/test_*.py``, ``tests/integration/``) drives the
  full FastAPI app (``app.api:app``) through ``TestClient`` *without* entering it
  as a context manager, so the lifespan (Neo4j check + background consumers)
  never runs and no real services are needed. Auth is satisfied by overriding
  ``verify_token``; Neo4j/Redis side-effects are mocked per-test. Fixtures:
  ``caller``, ``client``, ``mock_rate_limit``, ``mock_kind3_write``.

- The Open Ranking E2E suite (``tests/open_ranking/``) boots a *minimal* app that
  mounts only the Open Ranking router; its ``app``/``client``/``auth_headers``
  fixtures live in ``tests/open_ranking/conftest.py``. It reuses the NWT helpers
  (``make_nwt``) defined here.

Settings env is populated here, BEFORE any ``app.*`` import, because
``app.core.config.settings`` is constructed at import time.
"""

import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
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
    # Default observer perspective for the Open Ranking endpoints (their global
    # algorithms resolve the observer to this pubkey).
    "PERIODIC_GRAPERANK_PUBKEY": (
        "be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a"
    ),
}
for _key, _value in _DUMMY_ENV.items():
    os.environ.setdefault(_key, _value)

# Ensure the project root is importable when pytest is invoked from elsewhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import Request
from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Tag

from app.api import app
from app.utils.api_validators import verify_token
from app.utils.auth.auth_models import JWTData


# ---------------------------------------------------------------------------
# Event-building helpers (importable: `from tests.conftest import ...`)
# ---------------------------------------------------------------------------
def signed_event(
    keys: Keys, kind: int, follow_pubkeys: list[str] | None = None
) -> dict:
    """Build and sign a Nostr event, returned as the plain dict the API accepts."""
    tags = [Tag.parse(["p", pk]) for pk in (follow_pubkeys or [])]
    event = EventBuilder(Kind(kind), "").tags(tags).sign_with_keys(keys)
    return json.loads(event.as_json())


def signed_kind3(keys: Keys, follow_pubkeys: list[str]) -> dict:
    return signed_event(keys, 3, follow_pubkeys)


# ---------------------------------------------------------------------------
# NWT helpers — shared with the Open Ranking suite
# ---------------------------------------------------------------------------
_SENTINEL_OMIT = object()

# Audience the test app expects. Matches the hostname of PUBLIC_BASE_URL set in
# _DUMMY_ENV above (the NWT verifier derives `aud` from that).
TEST_AUD = "localhost"


def make_nwt(
    *,
    keys: Keys | None = None,
    aud: str | list[str] | None = TEST_AUD,
    exp=_SENTINEL_OMIT,
    nbf: int | None = None,
    iat: int | None = None,
    kind: int = 27519,
) -> tuple[str, str]:
    """Build a signed NWT, return (base64url_token, signer_hex_pubkey).

    - `aud` may be a string (single tag), a list (one tag per element), or
      None (no aud tag).
    - `exp` defaults to ~1 hour in the future. Pass `None` to OMIT it.
    """
    import base64

    keys = keys or Keys.generate()
    tags: list[Tag] = []

    if aud is not None:
        aud_list = [aud] if isinstance(aud, str) else aud
        for a in aud_list:
            tags.append(Tag.parse(["aud", a]))

    if exp is _SENTINEL_OMIT:
        exp = int(time.time()) + 3600
    if exp is not None:
        tags.append(Tag.parse(["exp", str(exp)]))
    if nbf is not None:
        tags.append(Tag.parse(["nbf", str(nbf)]))
    if iat is not None:
        tags.append(Tag.parse(["iat", str(iat)]))

    event = EventBuilder(Kind(kind), "").tags(tags).sign_with_keys(keys)
    token = (
        base64.urlsafe_b64encode(event.as_json().encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    return token, keys.public_key().to_hex()


# ---------------------------------------------------------------------------
# Fast-suite fixtures (full app.api:app + mocked side-effects)
# ---------------------------------------------------------------------------
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
