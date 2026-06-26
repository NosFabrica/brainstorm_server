"""Shared fixtures for the Open Ranking E2E tests.

The tests boot a real FastAPI app containing the Open Ranking router and
exercise it through `fastapi.testclient.TestClient` (i.e. real ASGI dispatch,
real Pydantic validation, real exception handlers, real CORS). External data
sources (Neo4j, Redis, Vespa) are stubbed at module-import boundaries so the
suite runs without a live infra stack.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings env: must be populated BEFORE any `app.*` module is imported,
# because `app.core.config.settings` is constructed at import time.
# ---------------------------------------------------------------------------
_REQUIRED_ENV = {
    "DB_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "DEPLOY_ENVIRONMENT": "LOCAL",
    "AUTH_ALGORITHM": "HS256",
    "AUTH_SECRET_KEY": "test-secret",
    "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES": "60",
    "SQL_ADMIN_USERNAME": "admin",
    "SQL_ADMIN_PASSWORD": "admin",
    "SQL_ADMIN_SECRET_KEY": "admin-secret",
    "NEO4J_DB_URL": "bolt://localhost:7687",
    "NEO4J_DB_USERNAME": "neo4j",
    "NEO4J_DB_PASSWORD": "password",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "NOSTR_TRANSFER_FROM_RELAY": "wss://example.invalid",
    "NOSTR_TRANSFER_TO_RELAY": "wss://example.invalid",
    "NOSTR_UPLOAD_TA_EVENTS_RELAY": "wss://example.invalid",
    "NOSTR_UPLOAD_TA_EVENTS_RELAY_PUBLIC_URL": "wss://example.invalid",
    "CUTOFF_OF_VALID_GRAPERANK_SCORES": "0.05",
    "PERFORM_NOSTR_FULL_SYNC": "false",
    "FRONTEND_URL": "http://localhost:3000",
    "PUBLIC_BASE_URL": "http://localhost:8000",
    "VESPA_URL": "http://localhost:8080",
    "PERIODIC_GRAPERANK_PUBKEY": "be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a",
}

for _k, _v in _REQUIRED_ENV.items():
    os.environ.setdefault(_k, _v)

# Ensure the project root is importable when pytest is invoked from elsewhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nostr_sdk import EventBuilder, Keys, Kind, Tag  # noqa: E402


# ---------------------------------------------------------------------------
# App factory: only mounts the Open Ranking router. Keeps tests independent
# of the brainstorm_server lifespan / background tasks.
# ---------------------------------------------------------------------------
def _build_app() -> FastAPI:
    from app.routers.open_ranking.router import router as open_ranking_router

    app = FastAPI()
    # Mirror the CORS posture of the production app so OPTIONS preflight tests
    # behave the same here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(open_ranking_router)
    return app


@pytest.fixture()
def app() -> FastAPI:
    return _build_app()


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """Convenience: a fresh, valid NWT for every test that needs to call an
    authenticated endpoint. The signing key is regenerated per-test so token
    reuse across cases can't accidentally pass.
    """
    # Lazy import: make_nwt is defined below in this module.
    token, _pk = make_nwt()
    return {"Authorization": f"Nostr {token}"}


# ---------------------------------------------------------------------------
# NWT helpers
# ---------------------------------------------------------------------------
_SENTINEL_OMIT = object()


# Audience the test app expects. Matches the hostname of PUBLIC_BASE_URL set
# in _REQUIRED_ENV above (the NWT verifier derives `aud` from that).
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
